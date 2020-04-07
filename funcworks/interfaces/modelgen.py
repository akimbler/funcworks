# pylint: disable=C0415,C0114,C0115,W0404,W0621,W0612
from pathlib import Path
from bids.layout.writing import build_path
from nipype.interfaces.base import (
    BaseInterfaceInputSpec, Bunch, TraitedSpec,
    InputMultiPath, OutputMultiPath, File,
    traits, Directory)
from nipype.interfaces.io import IOBase
import nibabel as nb
import numpy as np
from ..utils import snake_to_camel


class GetRunModelInfoInputSpec(BaseInterfaceInputSpec):
    bids_dir = Directory(exists=True, mandatory=True)
    functional_file = File(exists=True, mandatory=True)
    model = traits.Dict(mandatory=True)
    detrend_poly = traits.Any(
        default=None,
        desc=('Legendre polynomials to regress out'
              'for temporal filtering'))
    align_volumes = traits.Any(
        default=None,
        desc=('Target volume for functional realignment',
              'if not value is specified, will not functional file'))


class GetRunModelInfoOutputSpec(TraitedSpec):
    run_info = traits.Any(
        desc='Model Info required to construct Run Level Model')
    run_contrasts = traits.List(
        desc='List of tuples describing each contrasts')
    run_entities = traits.Dict(desc='Run specific BIDS Entities')
    contrast_entities = OutputMultiPath(
        traits.Dict(),
        desc='Contrast specific list of entities')
    motion_parameters = OutputMultiPath(
        File(exists=True),
        desc='File containing first six motion regressors')
    repetition_time = traits.Float(desc='Repetition Time for the dataset')
    contrast_names = traits.List(
        desc='List of Contrast Names to pass to higher levels')
    reference_image = File(
        exists=True,
        desc='Reference Image for functional realignment')
    brain_mask = File(exists=True, desc='Brain mask for functional image')


class GetRunModelInfo(IOBase):
    '''Grabs EV files for subject based on contrasts of interest'''
    input_spec = GetRunModelInfoInputSpec
    output_spec = GetRunModelInfoOutputSpec
    # _always_run = True

    def _list_outputs(self):
        import json
        import pandas as pd

        outputs = {}
        (regressor_file, meta_file, events_file,
         outputs['reference_image'], outputs['brain_mask'],
         outputs['run_entities']) = self._get_required_files()

        outputs['motion_parameters'] = self._get_motion_parameters(
            regressor_file=regressor_file)

        with open(meta_file, 'r') as meta_read:
            run_metadata = json.load(meta_read)
        outputs['repetition_time'] = run_metadata['RepetitionTime']
        (outputs['run_info'], event_regressors,
         confound_regressors) = self._get_model_info(
             events_file=events_file, regressor_file=regressor_file)
        (outputs['run_contrasts'],
         outputs['contrast_names']) = self._get_contrasts(
             event_names=event_regressors)
        all_regressors = event_regressors + confound_regressors
        n_vols = len(pd.read_csv(regressor_file))
        outputs['run_entities'].update({
            'Volumes': n_vols,
            'DegreesOfFreedom': (n_vols - len(all_regressors))})
        outputs['contrast_entities'] = self._get_entities(
            contrasts=outputs['run_contrasts'],
            run_entities=outputs['run_entities'])

        if self.inputs.detrend_poly:
            polynomial_names, polynomial_arrays = self._detrend_polynomial(
                regressor_file, self.inputs.detrend_poly)
            outputs['run_info'].regressor_names.extend(polynomial_names)
            outputs['run_info'].regressors.extend(polynomial_arrays)

        return outputs

    def _get_required_files(self):
        # A workaround to a current issue in pybids
        # that causes massive resource use when indexing derivative tsv files
        from pathlib import Path
        from bids.layout import parse_file_entities
        from bids.layout.writing import build_path
        func = Path(self.inputs.functional_file)
        entities = parse_file_entities(str(func))
        entities.update({'run': '{:02}'.format(entities['run'])})
        confounds_patt = ('sub-{subject}_[ses-{session}_]task-{task}_'
                          'run-{run}_desc-confounds_regressors.tsv')
        meta_patt = ('sub-{subject}_[ses-{session}_]task-{task}_'
                     'run-{run}_[space-{space}_]desc-preproc_bold.json')
        events_patt = ('sub-{subject}/[ses-{session}/]{datatype}/'
                       'sub-{subject}_[ses-{session}_]'
                       'task-{task}_run-{run}_events.tsv')
        ref_patt = ('sub-{subject}_[ses-{session}_]task-{task}_'
                    'run-{run}_[space-{space}_]boldref.nii.gz')
        mask_patt = ('sub-{subject}_[ses-{session}_]task-{task}_'
                     'run-{run}_[space-{space}_]desc-brain_mask.nii.gz')
        regressor_file = (func.parent
                          / build_path(entities, path_patterns=confounds_patt))
        meta_file = (func.parent
                     / build_path(entities, path_patterns=meta_patt))
        events_file = (Path(self.inputs.bids_dir)
                       / build_path(entities, path_patterns=events_patt))
        ents = entities.copy()
        if self.inputs.align_volumes:
            ents.update({'run': '{:02d}'.format(self.inputs.align_volumes)})
        reference_image = (func.parent
                           / build_path(ents, path_patterns=ref_patt))
        mask_image = (func.parent
                      / build_path(ents, path_patterns=mask_patt))
        return (regressor_file, meta_file, events_file,
                reference_image, mask_image, entities)

    def _get_model_info(self, events_file, regressor_file):
        import pandas as pd
        import numpy as np

        event_data = pd.read_csv(events_file, sep='\t')
        conf_data = pd.read_csv(regressor_file, sep='\t')
        conf_data.fillna(0, inplace=True)
        level_model = self.inputs.model
        run_info = {'conditions': [],
                    'onsets': [],
                    'amplitudes': [],
                    'durations': [],
                    'regressor_names': [],
                    'regressors': []}
        for regressor in level_model['Model']['X']:
            if '.' in regressor:
                event_column, event_name = regressor.split('.')
                event_frame = event_data.query(
                    f'{event_column} == "{event_name}"')
                if event_frame.empty:
                    continue
                run_info['conditions'].append(regressor)
                run_info['onsets'].append(event_frame['onset'].values)
                run_info['durations'].append(event_frame['duration'].values)
                run_info['amplitudes'].append(np.ones(len(event_frame)))
            else:
                run_info['regressor_names'].append(regressor)
                run_info['regressors'].append(conf_data[regressor].values)

        run_info = Bunch(**run_info)
        return (run_info,
                run_info.conditions,  # pylint: disable=E1101
                run_info.regressor_names)  # pylint: disable=E1101

    def _get_contrasts(self, event_names):
        """
        Produces contrasts from a given model file
        and a run specific events file
        """
        model = self.inputs.model
        contrast_spec = []
        real_contrasts = model["Contrasts"]
        contrast_names = []
        dummy_contrasts = []
        if 'Conditions' in model["DummyContrasts"]:
            dummy_contrasts = model["DummyContrasts"]['Conditions']
        else:
            dummy_contrasts = model["Model"]["X"]

        for dcontrast in dummy_contrasts:
            if dcontrast not in event_names and '.' in dcontrast:
                continue
            contrast_spec.append((dcontrast, 'T', [dcontrast], [1]))
            contrast_names.append(dcontrast)

        for contrast in real_contrasts:
            if not set(event_names).issubset(contrast['ConditionList']):
                continue
            contrast_names.append(contrast['Name'])
            if contrast['Name'] == 'task_vs_baseline':
                weight_vector = [1 * 1 / len(event_names)] * len(event_names)
                contrast_spec.append((contrast['Name'],
                                      contrast['Type'].upper(),
                                      event_names,
                                      weight_vector))
            else:
                contrast_spec.append((contrast['Name'],
                                      contrast['Type'].upper(),
                                      contrast['ConditionList'],
                                      contrast['Weights']))
        return contrast_spec, contrast_names

    @staticmethod
    def _get_motion_parameters(regressor_file):
        from pathlib import Path
        import pandas as pd
        regressor_file = Path(regressor_file)
        motparams_path = (Path.cwd()
                          / str(regressor_file.name).replace('regressors',
                                                             'motparams'))

        confound_data = pd.read_csv(regressor_file, sep='\t')
        # Motion data gets formatted FSL style, with x, y, z rotation,
        # then x,y,z translation
        motion_data = confound_data[['rot_x', 'rot_y', 'rot_z',
                                     'trans_x', 'trans_y', 'trans_z']]
        motion_data.to_csv(motparams_path, sep='\t', header=None, index=None)
        motion_params = motparams_path
        return motion_params

    @staticmethod
    def _get_entities(contrasts, run_entities):
        contrast_entities = []
        contrast_names = [contrast[0] for contrast in contrasts]
        for contrast_name in contrast_names:
            run_entities.update({'contrast': contrast_name})
            contrast_entities.append(run_entities.copy())
        return contrast_entities

    @staticmethod
    def _detrend_polynomial(regressor_file, detrend_poly=None):
        import numpy as np
        import pandas as pd
        from scipy.special import legendre

        regressors_frame = pd.read_csv(regressor_file)

        poly_names = []
        poly_arrays = []
        for i in range(0, detrend_poly + 1):
            poly_names.append(f'legendre{i:02d}')
            poly_arrays.append(
                legendre(i)(np.linspace(-1, 1, len(regressors_frame))))

        return poly_names, poly_arrays


class GenerateHigherInfoInputSpec(BaseInterfaceInputSpec):
    contrast_maps = InputMultiPath(
        File(exists=True), desc='List of statmaps from previous level')
    contrast_metadata = InputMultiPath(
        traits.Dict(desc='Contrast names inherited from previous levels'))
    model = traits.Dict(desc='Step level information from the model file')
    derivatives = Directory(desc='fmriPrep derivatives directory')
    align_volumes = traits.Any(
        default=None,
        desc=('Target volume for functional realignment',
              'if not value is specified, will not functional file'))


class GenerateHigherInfoOutputSpec(TraitedSpec):
    effect_maps = traits.List()
    variance_maps = traits.List()
    dof_maps = traits.List()
    contrast_matrices = traits.List()
    design_matrices = traits.List()
    covariance_matrices = traits.List()
    contrast_metadata = traits.List()
    brain_mask = traits.List()


class GenerateHigherInfo(IOBase):
    input_spec = GenerateHigherInfoInputSpec
    output_spec = GenerateHigherInfoOutputSpec

    _always_run = True

    def _list_outputs(self):
        organization, dummy_contrasts = self._get_organization()
        (contrast_entities, effect_maps,
         variance_maps, dof_maps,
         brain_masks) = self._merge_maps(
             organization, dummy_contrasts, self.inputs.derivatives,
             self.inputs.align_volumes)
        design_matrices, contrast_matrices, covariance_matrices = \
            self._produce_matrices(contrast_entities=contrast_entities)
        return {'effect_maps': effect_maps,
                'variance_maps': variance_maps,
                'dof_maps': dof_maps,
                'contrast_metadata': contrast_entities,
                'contrast_matrices': contrast_matrices,
                'design_matrices': design_matrices,
                'covariance_matrices': covariance_matrices,
                'brain_mask': brain_masks}

    def _get_organization(self):
        model = self.inputs.model

        contrast_zip = zip(self.inputs.contrast_maps,
                           self.inputs.contrast_metadata)
        organization = {}
        # split_fields = []
        # if "Transformations" in model:
        #     if model['Transformations']['Name'] == 'Split':
        #         split_fields = []
        for contrast_file, contrast_entities in contrast_zip:
            if contrast_entities['contrast'] not in organization:
                organization[contrast_entities['contrast']] = []
            organization[contrast_entities['contrast']].append(
                {'File': contrast_file, 'Metadata': contrast_entities})

        # for split_field in split_fields:
        #     pass
        dummy_contrasts = []
        if "DummyContrasts" in model:
            if 'Conditions' in model['DummyContrasts']:
                dummy_contrasts = model['DummyContrasts']['Conditions']
            else:
                dummy_contrasts = organization.keys()
        return organization, dummy_contrasts

    def _merge_maps(self, organization, dummy_contrasts,
                    derivatives, align_volumes=None):
        maps_info = {'effect_maps': [],
                     'dof_maps': [],
                     'variance_maps': [],
                     'map_entities': [],
                     'mask_files': []}
        mask_patt = ('sub-{subject}[_ses-{session}]_task-{task}'
                     '_run-{run}[_space-{space}]_desc-brain_mask.nii.gz')
        for dcontrast in dummy_contrasts:
            dcontrast_info = {'dceffect_maps': [],
                              'dcvariance_maps': [],
                              'dcdof_maps': [],
                              'dcentities': {}}
            for bids_info in organization[dcontrast]:
                if 'stat' not in bids_info['Metadata']:
                    continue
                if bids_info['Metadata']['stat'] == 'effect':
                    open_file = nb.load(bids_info['File'])
                    affine = open_file.affine
                    dof_file = (np.ones_like(open_file.get_fdata())
                                * bids_info['Metadata']['DegreesOfFreedom'])
                    dof_file = nb.nifti1.Nifti1Image(dof_file, affine)
                    dcontrast_info['dceffect_maps'].append(open_file)
                    dcontrast_info['dcdof_maps'].append(dof_file)
                elif bids_info['Metadata']['stat'] == 'variance':
                    open_file = nb.load(bids_info['File'])
                    dcontrast_info['dcvariance_maps'].append(open_file)
                dcontrast_info['dcentities'] = bids_info['Metadata'].copy()
            for statmap in ['dceffect_maps', 'dcvariance_maps', 'dcdof_maps']:
                merged_statmap = nb.concat_images(dcontrast_info[statmap])
                dcontrast_info[statmap] = merged_statmap
                if statmap == 'dceffect_maps':
                    dcontrast_info['dcentities'].update({
                        'NumLevelTimepoints': merged_statmap.shape[-1]})
            dcontrast_info['dcentities'].pop('stat', None)
            maps_info['map_entities'].append(dcontrast_info['dcentities'])

            ents = dcontrast_info['dcentities'].copy()
            ents.pop('run', None)
            ents.update({
                'contrast': snake_to_camel(
                    dcontrast_info['dcentities']['contrast'])})
            merged_patt = ('sub-{subject}[_ses-{session}]'
                           '_contrast-{contrast}_stat-{stat}'
                           '_desc-merged_statmap.nii.gz')
            for stat in ['effect', 'variance', 'dof']:
                ents['stat'] = stat
                map_path = (Path.cwd()
                            / build_path(ents, path_patterns=merged_patt))
                nb.nifti1.save(dcontrast_info[f'dc{stat}_maps'], map_path)
                maps_info[f'{stat}_maps'].append(str(map_path))
            if not align_volumes:
                ents.update({'run': 1})
            else:
                ents.update({'run': align_volumes})
            mask_path = (Path(derivatives)
                         / build_path(ents, path_patterns=mask_patt))
            maps_info['mask_files'].append(mask_path)
        return (maps_info['map_entities'], maps_info['effect_maps'],
                maps_info['variance_maps'], maps_info['dof_maps'],
                maps_info['mask_files'])

    def _produce_matrices(self, contrast_entities):
        matrix_paths = {'design_matrices': [],
                        'contrast_matrices': [],
                        'covariance_matrices': []}

        header_lines = {'contrast': ['/ContrastName1 {contrast}\n',
                                     '/NumWaves 1\n',
                                     '/NumPoints 1\n\n',
                                     '/Matrix\n',
                                     '1\n'],
                        'design': ['/NumWaves 1\n',
                                   '/NumPoints {numcopes}\n',
                                   '/PPHeights 1\n\n',
                                   '/Matrix\n'],
                        'covariance': ['/NumWaves 1\n',
                                       '/NumPoints {numcopes}\n\n',
                                       '/Matrix\n']}
        matrix_patt = ('sub-{subject}[ses-{session}_]'
                       'contrast-{contrast}_desc-{desc}_design.mat')
        for entity in contrast_entities:
            ents = entity.copy()
            numcopes = ents['NumLevelTimepoints']
            for matrix_type in ['design', 'contrast', 'covariance']:
                ents.update({'desc': 'contrast'})
                matrix_path = (Path.cwd()
                               / build_path(ents, path_patterns=matrix_patt))
                contrast = ents['contrast']
                ents['contrast'] = snake_to_camel(entity['contrast'])
                mat_file = open(matrix_path, 'a')
                for header_line in header_lines[matrix_type]:
                    mat_file.writelines(
                        header_line.format(contrast=contrast,
                                           numcopes=numcopes))
                if matrix_type == 'covariance':
                    for _ in range(numcopes):
                        mat_file.writelines('1\n')
                mat_file.close()
                matrix_paths[f'{matrix_type}_matrices'].append(
                    str(matrix_path))
        return (matrix_paths['design_matrices'],
                matrix_paths['contrast_matrices'],
                matrix_paths['covariance_matrices'])
