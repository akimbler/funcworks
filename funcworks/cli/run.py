#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Main run script."""
import gc
import sys
import uuid
import json
import logging
import warnings
from pathlib import Path
from tempfile import mkdtemp
from time import strftime
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
from .. import __version__

logging.addLevelName(25, "IMPORTANT")  # New level between INFO and WARNING
logging.addLevelName(15, "VERBOSE")  # New level between INFO and DEBUG
logger = logging.getLogger("cli")


def check_deps(workflow):
    """Make sure workflow dependencies are present before runtime."""
    from nipype.utils.filemanip import which

    return sorted(
        (node.interface.__class__.__name__, node.interface._cmd)
        for node in workflow._get_all_nodes()
        if (hasattr(node.interface, "_cmd") and which(node.interface._cmd.split()[0]) is None)
    )


def _warn_redirect(message, category, filename, lineno, file=None, line=None):
    logger.warning("Captured warning (%s): %s", category, message)


def get_parser():
    """Build Parser Object."""
    parser = ArgumentParser(
        description="FUNCWORKs: fMRI FUNCtional WORKflows",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )

    # Required Arguments
    parser.add_argument(
        "bids_dir",
        action="store",
        type=Path,
        help="Root folder of BIDS Dataset being analyzed"
        "(sub-XXXXX folders should be found at the top level in this folder).",
    )
    parser.add_argument(
        "output_dir",
        action="store",
        type=Path,
        help="Output path for the processed data and visual reports",
    )
    parser.add_argument(
        "analysis_level",
        choices=["run", "session", "participant", "dataset"],
        help="Processing stage to be run (see BIDS-Apps specification).",
    )

    # Optional arguments
    parser.add_argument(
        "-w",
        "--work-dir",
        action="store",
        type=Path,
        help="Path where intermediate results should be stored.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    g_bids = parser.add_argument_group("Options to specify bids entities")
    g_bids.add_argument(
        "-m", "--model-file", action="store", type=Path, help="Location of BIDS model file",
    )
    g_bids.add_argument(
        "-d",
        "--derivatives",
        action="store",
        nargs="+",
        help="Location containing fMRIPrep preprocessed images.",
    )
    g_bids.add_argument(
        "--participant_label",
        "--participant-label",
        action="store",
        nargs="+",
        help="Space delimited list of participant identifiers or a single "
        "identifier (the sub- prefix can be removed)",
    )
    g_bids.add_argument(
        "--database-path",
        action="store",
        default=None,
        help="Path to existing directory containing BIDS "
        "Database files useful for speeding up run-time.",
    )

    g_prep = parser.add_argument_group("Options for preprocessing data")
    g_prep.add_argument(
        "-s",
        "--smoothing",
        action="store",
        metavar="FWHM[:LEVEL:[TYPE]]",
        default=None,
        help=(
            "Smooth BOLD series with FWHM mm kernel prior to fitting. "
            "Optional analysis LEVEL (default: l1) is specified by level "
            "(`l1`) or name (`run`, `subject`, `session` or `dataset`). "
            "Optional smoothing TYPE (default: iso) must be one of:"
            "`iso` (isotropic). "
            "e.g., `--smoothing 5:run:iso` will perform a 5mm FWHM isotropic "
            "smoothing on run-level maps before evaluating the dataset level."
        ),
    )
    g_prep.add_argument(
        "--align-volumes",
        action="store",
        default=None,
        type=int,
        help="Bold reference to align timeseries, "
        "this will override any run specific entities "
        "or specifications in the model file "
        "for the boldref and brain_mask.",
    )
    g_prep.add_argument(
        "--use-rapidart",
        action="store_true",
        default=False,
        help="Use RapidArt artifact detection algorithm.",
    )
    g_prep.add_argument(
        "--detrend-poly",
        action="store",
        default=None,
        type=int,
        help="Legendre polynomials to use for temporal filtering.",
    )
    g_prep.add_argument(
        "-sa",
        "--smooth-autocorrelations",
        action="store_true",
        default=False,
        help="Option to enable smoothing of autocorrelations " "during run level analyses.",
    )
    g_prep.add_argument(
        "--despike", default=False, action="store_true", help="Run afni despike on the data",
    )

    g_perf = parser.add_argument_group("Options to impact performance")
    g_perf.add_argument(
        "--use-plugin",
        action="store",
        default=None,
        help="File containing plugin configuration for Nipype.",
    )
    g_perf.add_argument(
        "--resource-monitor",
        dest="resource_monitor",
        action="store_true",
        help="Use Nipype resource monitoring.",
    )
    return parser


def main():
    """Entry Point."""
    from multiprocessing import set_start_method, Process, Manager

    set_start_method("spawn")
    warnings.showwarning = _warn_redirect

    opts = get_parser().parse_args()
    # exec_env = os.name

    # sentry_sdk = None
    #
    # if not opts.notrack:
    #     import sentry_sdk
    #     from ..utils.sentry import sentry_setup
    #     sentry_setup(opts, exec_env)

    if opts.analysis_level not in ["run", "session", "participant", "dataset"]:
        raise ValueError(
            (
                f"Unknown analysis level {opts.analysis_level}",
                "analysis level must be  one of ",
                "'run', 'session', 'participant', 'dataset'",
            )
        )
    with Manager() as mgr:
        retval = mgr.dict()

        p = Process(target=build_workflow, args=(opts, retval))
        p.start()
        p.join()

        retcode = p.exitcode or retval.get("return_code", 0)
        # bids_dir = retval.get('bids_dir')
        # output_dir = retval.get('output_dir')
        # work_dir = retval.get('work_dir')
        # subject_list = retval.get('participant_label', None)
        # runtime_uuid = retval.get('runtime_uuid', None)
        plugin_settings = retval.get("plugin_settings")
        funcworks_wf = retval.get("workflow", None)

    retcode = retcode or int(funcworks_wf is None)
    if retcode != 0:
        sys.exit(retcode)

    missing = check_deps(funcworks_wf)
    if missing:
        error_msg = "Cannot run FUNCWorks. Missing dependencies:\n"
        error_msg += "".join([f"\t{cmd} (Interface: {iface})" for iface, cmd in missing])
        raise ModuleNotFoundError(error_msg)
    # Clean up master process before running workflow, which may create forks
    gc.collect()
    # errno = 1
    # Default is error exit unless otherwise set
    try:
        funcworks_wf.write_graph(graph2use="colored", format="png")
    except Exception as e:
        logger.warning(f"Attempt to write graph failed: {e}")
    try:
        funcworks_wf.run(**plugin_settings)
    except Exception as e:
        #
        # if not opts.notrack:
        #     from ..utils.sentry import process_crashfile
        #     crashfolders = [
        #         output_dir / 'funcworks' / 'sub-{}'.format(s) /
        #         'log' / runtime_uuid for s in subject_list]
        #    for crashfolder in crashfolders:
        #         for crashfile in crashfolder.glob('crash*.*'):
        #             process_crashfile(crashfile)
        #
        #    if "Workflow did not execute cleanly" not in str(e):
        #         sentry_sdk.capture_exception(e)
        logger.critical(f"FUNCWorks failed: {e}")
        raise


def build_workflow(opts, retval):
    """
    Create the Nipype Workflow for a graph given the inputs.

    All the checks and the construction of the workflow are done
    inside this function that has pickleable inputs and output
    dictionary (``retval``) to allow isolation using a
    ``multiprocessing.Process`` that allows funcworks to enforce
    a hard-limited memory-scope.
    """
    from bids import BIDSLayout

    from nipype import logging as nlogging, config as ncfg
    from ..workflows.base import init_funcworks_wf
    from .. import __version__

    build_log = nlogging.getLogger("nipype.workflow")

    output_dir = opts.output_dir.resolve()
    bids_dir = opts.bids_dir.resolve()
    work_dir = mkdtemp() if opts.work_dir is None else opts.work_dir.resolve()
    retval["return_code"] = 1
    retval["workflow"] = None
    retval["bids_dir"] = bids_dir
    retval["output_dir"] = output_dir
    retval["work_dir"] = work_dir

    if not opts.database_path:
        database_path = str(opts.work_dir.resolve() / "dbcache")
        layout = BIDSLayout(
            bids_dir,
            derivatives=opts.derivatives,
            validate=True,
            database_file=database_path,
            reset_database=True,
        )
    else:
        database_path = opts.database_path
        layout = BIDSLayout.load(database_path)

    if output_dir == bids_dir:
        build_log.error(
            "The selected output folder is the same as the input BIDS folder. "
            "Please modify the output path (suggestion: %s).",
            (bids_dir / "derivatives" / ("funcworks-%s" % __version__.split("+")[0])),
        )
        retval["return_code"] = 1
        return retval

    if bids_dir in opts.work_dir.parents:
        build_log.error(
            "The selected working directory is a subdirectory "
            "of the input BIDS folder. "
            "Please modify the output path."
        )
        retval["return_code"] = 1
        return retval

    # Set up some instrumental utilities
    runtime_uuid = "%s_%s" % (strftime("%Y%m%d-%H%M%S"), uuid.uuid4())
    retval["runtime_uuid"] = runtime_uuid

    if opts.participant_label:
        retval["participant_label"] = opts.participant_label
    else:
        retval["participant_label"] = layout.get_subjects()

    # Load base plugin_settings from file if --use-plugin
    plugin_settings = {
        "plugin": "MultiProc",
        "plugin_args": {"raise_insufficient": False, "maxtasksperchild": 1},
    }
    if opts.use_plugin is not None:
        with open(opts.use_plugin) as f:
            plugin_settings = json.load(f)

    # Resource management options
    # Note that we're making strong assumptions about valid plugin args
    # This may need to be revisited if people try to use batch plugins
    # nthreads = plugin_settings['plugin_args'].get('n_procs')
    # Permit overriding plugin config with specific CLI options
    # if nthreads is None or opts.nthreads is not None:
    #    nthreads = opts.nthreads
    #    if nthreads is None or nthreads < 1:
    #        nthreads = cpu_count()
    #    plugin_settings['plugin_args']['n_procs'] = nthreads
    # if opts.mem_mb:
    #    plugin_settings['plugin_args']['memory_gb'] = opts.mem_mb / 1024
    # omp_nthreads = opts.omp_nthreads
    # if omp_nthreads == 0:
    #    omp_nthreads = min(nthreads - 1 if nthreads > 1 else cpu_count(), 8)
    # if 1 < nthreads < omp_nthreads:
    #    build_log.warning(
    #        'Per-process threads (--omp-nthreads=%d) exceed total '
    #        'threads (--nthreads/--n_cpus=%d)', omp_nthreads, nthreads)
    retval["plugin_settings"] = plugin_settings

    # Set up directories
    # Check and create output and working directories
    output_dir.mkdir(exist_ok=True, parents=True)
    work_dir.mkdir(exist_ok=True, parents=True)

    # Nipype config (logs and execution)
    ncfg.update_config(
        {
            "logging": {"log_to_file": True},
            "execution": {
                "crashfile_format": "txt",
                "get_linked_libs": False,
                # 'stop_on_first_crash': opts.stop_on_first_crash,
            },
            "monitoring": {
                "enabled": opts.resource_monitor,
                "sample_frequency": "0.5",
                "summary_append": True,
            },
        }
    )

    if opts.resource_monitor:
        ncfg.enable_resource_monitor()
    # Called with reports only
    # if opts.reports_only:
    #     build_log.log(25, 'Running --reports-only on participants %s',
    #                   ', '.join(opts.participant_label))
    #     if opts.runtime_uuid is not None:
    #         runtime_uuid = opts.runtime_uuid
    #         retval['runtime_uuid'] = runtime_uuid
    #     retval['return_code'] = generate_reports(
    #         opts.participant_label, output_dir, work_dir, runtime_uuid,
    #         packagename='funcworks')
    #     return retval

    # Build main workflow
    build_log.log(
        25,
        (
            f"""
        Running FUNCWORKS version {__version__}:
          * BIDS dataset path: {bids_dir}.
          * Participant list: {retval['participant_label']}.
          * Run identifier: {runtime_uuid}.
        """
        ),
    )

    if not opts.model_file:
        model_file = Path(bids_dir) / "models" / "model-default_smdl.json"
        if not model_file.exists():
            raise ValueError("Default Model File not Found")
    else:
        model_file = opts.model_file

    retval["workflow"] = init_funcworks_wf(
        model_file=model_file,
        bids_dir=opts.bids_dir,
        output_dir=opts.output_dir,
        work_dir=opts.work_dir,
        database_path=str(database_path),
        participants=retval["participant_label"],
        analysis_level=opts.analysis_level,
        smoothing=opts.smoothing,
        runtime_uuid=runtime_uuid,
        use_rapidart=opts.use_rapidart,
        detrend_poly=opts.detrend_poly,
        align_volumes=opts.align_volumes,
        smooth_autocorrelations=opts.smooth_autocorrelations,
        despike=opts.despike,
    )

    retval["return_code"] = 0
    """
    logs_path = Path(output_dir) / 'funcworks' / 'logs'
    boilerplate = retval['workflow'].visit_desc()

    if boilerplate:
        citation_files = {
            ext: logs_path / ('CITATION.%s' % ext)
            for ext in ('bib', 'tex', 'md', 'html')
        }
        # To please git-annex users and also to guarantee consistency
        # among different renderings of the same file, first remove any
        # existing one
        for citation_file in citation_files.values():
            try:
                citation_file.unlink()
            except FileNotFoundError:
                pass

        citation_files['md'].write_text(boilerplate)
        build_log.log(25, 'Works derived from this FUNCWorks execution should '
                      'include the following boilerplate:\n\n%s', boilerplate)
    """
    return retval


if __name__ == "__main__":
    main()
