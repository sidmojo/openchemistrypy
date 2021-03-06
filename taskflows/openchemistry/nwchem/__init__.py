import cumulus
from cumulus.taskflow import TaskFlow
from cumulus.taskflow.cluster import create_girder_client
from cumulus.tasks.job import (download_job_input_folders,
                               upload_job_output_to_folder)
from cumulus.tasks.job import submit_job, monitor_job

from girder.api.rest import getCurrentUser
from girder.constants import AccessType
from girder.utility.model_importer import ModelImporter

from jsonpath_rw import parse
import os
import datetime
import json
from io import BytesIO
import jinja2
import tempfile

def _jsonpath(path, json):
    values = [x.value for x in parse(path).find(json)]
    if len(values) != 1:
        raise Exception('Path did not resolve to single property.')

    return values[0]


class NWChemTaskFlow(TaskFlow):
    """
    {
        "input": {
            "calculation": {
                "_id": <the id of the pending calculation>
            },
        },
        "cluster": {
            "_id": <id of cluster to run on>
        }
    }
    """

    def start(self, *args, **kwargs):
        user = getCurrentUser()
        input_ = kwargs.get('input')
        cluster = kwargs.get('cluster')

        if input_ is None:
            raise Exception('Unable to extract input.')


        if '_id' not in cluster and 'name' not in cluster:
            raise Exception('Unable to extract cluster.')

        cluster_id = parse('cluster._id').find(kwargs)
        if cluster_id:
            cluster_id = cluster_id[0].value
            model = ModelImporter.model('cluster', 'cumulus')
            cluster = model.load(cluster_id, user=user, level=AccessType.ADMIN)
            cluster = model.filter(cluster, user, passphrase=False)

        super(NWChemTaskFlow, self).start(
            setup_input.s(input_, cluster),
            *args, **kwargs)

def _get_cori(client):
    params = {
        'type': 'newt'
    }
    clusters = client.get('clusters', parameters=params)
    for cluster in clusters:
        if cluster['name'] == 'cori':
            return cluster

    # We need to create one
    body = {
        'config': {
            'host': 'cori'
        },
        'name': 'cori',
        'type': 'newt'
    }
    cluster = client.post('clusters', data=json.dumps(body))

    return cluster

def _get_oc_folder(client):
    me = client.get('user/me')
    if me is None:
        raise Exception('Unable to get me.')

    login = me['login']
    private_folder_path =    'user/%s/Private' % login
    private_folder = client.resourceLookup(private_folder_path)
    oc_folder_path = '%s/oc' % private_folder_path
    oc_folder = client.resourceLookup(oc_folder_path, test=True)
    if oc_folder is None:
        oc_folder = client.createFolder(private_folder['_id'], 'oc')

    return oc_folder

def _fetch_best_geometry(client, molecule_id):
    # Fetch our best geometry
    params = {
        'moleculeId': molecule_id,
        'sortByTheory': True,
        'limit': 1,
        'calculationType': 'optimization',
        'pending': False
    }

    calculations = client.get('calculations', parameters=params)

    if len(calculations) < 1:
        return None

    return calculations[0]

@cumulus.taskflow.task
def setup_input(task, input_, cluster):
    client = create_girder_client(
        task.taskflow.girder_api_url, task.taskflow.girder_token)

    if cluster.get('name') == 'cori':
        cluster = _get_cori(client)

    if '_id' not in cluster:
        raise Exception('Invalid cluster configurations: %s' % cluster)

    optimize = input_['optimize']
    calculation_id = parse('calculation._id').find(input_)
    if not calculation_id:
        raise Exception('Unable to extract calculation id.')
    calculation_id = calculation_id[0].value
    calculation = client.get('calculations/%s' % calculation_id)
    molecule_id = calculation['moleculeId']

    optimization_calculation_id = None
    input_calculation = parse('properties.input.calculationId').find(calculation)
    # We have been asked to use a specific calculation
    if input_calculation:
        optimization_calculation_id = input_calculation[0].value
    # We have been asked to use a specific optimized geometry, see if we have it
    elif optimize:
        parameters = {
            'moleculeId': molecule_id,
            'calculationType': 'optimizations',
        }

        basis = parse('properties.basisSet.name').find(calculation)
        if basis:
            parameters['basis'] = basis[0].value

        functional = parse('properties.functional').find(calculation)
        if functional:
            parameters['functional'] = functional[0].value.lower()

        theory = parse('properties.theory').find(calculation)
        if theory:
            parameters['theory'] = theory[0].value.lower()


        calculations = client.get('calculations', parameters)

        if len(calculations) > 0:
            optimization_calculation_id = calculations[0]['_id']

    best_calc = None
    if optimization_calculation_id is None:
        best_calc = _fetch_best_geometry(client, molecule_id)

    # We are using a specific one
    if optimization_calculation_id is not None:
        r = client.get('calculations/%s/xyz' % optimization_calculation_id,
                       jsonResp=False)
        xyz = r.content
    # If we have not calculations then just use the geometry stored in molecules
    elif best_calc is None:
        r = client.get('molecules/%s/xyz' % molecule_id, jsonResp=False)
        xyz = r.content
        # As we might be using an unoptimized structure add the optimize step
        if 'optimization' not in calculation['properties']['calculationTypes']:
            calculation['properties']['calculationTypes'].append('optimization')
    # Fetch xyz for best geometry
    else:
        optimization_calculation_id = best_calc['_id']
        r = client.get('calculations/%s/xyz' % optimization_calculation_id,
                       jsonResp=False)
        xyz = r.content

    # If we are using an existing calculation as the input geometry record it
    if optimization_calculation_id is not None:
        props = calculation['properties']
        props['input'] = {
            'calculationId': optimization_calculation_id
        }
        calculation = client.put('calculations/%s/properties' % calculation['_id'],
                                 json=props)

    oc_folder = _get_oc_folder(client)
    run_folder = client.createFolder(oc_folder['_id'],
                                     datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%f"))
    input_folder = client.createFolder(run_folder['_id'],
                                       'input')

    # Generate input file
    params = {}
    calculation_types = parse('properties.calculationTypes').find(calculation)
    if calculation_types:
        calculation_types = calculation_types[0].value

    for calculation_type in calculation_types:
        params[calculation_type] = True

    # If we have been asked to use a optimized structure make sure we
    # run the optimization if we couldn't find calculation.
    if optimize and optimization_calculation_id is None:
        params['optimization'] = True

    basis = parse('properties.basisSet.name').find(calculation)
    if basis:
        params['basis'] = basis[0].value

    functional = parse('properties.functional').find(calculation)
    if functional:
        params['functional'] = functional[0].value.lower()

    theory = parse('properties.theory').find(calculation)
    if theory:
        params['theory'] = theory[0].value.lower()

    template_path = os.path.dirname(__file__)
    jinja2_env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_path),
                             trim_blocks=True)
    with tempfile.TemporaryFile() as fp:
        jinja2_env.get_template('oc.nw.j2').stream(**params).dump(fp, encoding='utf8')
        # Get the size of the file
        size = fp.seek(0, 2)
        fp.seek(0)
        name = 'oc.nw'
        input_file = client.uploadFile(input_folder['_id'],  fp, name, size,
                                       parentType='folder')
    # Upload the xyz file
    size = len(xyz)
    client.uploadFile(input_folder['_id'], BytesIO(xyz), 'geometry.xyz', size,
                      parentType='folder')

    submit.delay(input_, cluster, run_folder, input_file, input_folder)

def _create_job_ec2(task, cluster, input_file, input_folder):
    task.taskflow.logger.info('Create NWChem job.')
    input_name = input_file['name']

    body = {
        'name': 'nwchem_run',
        'commands': ['docker pull openchemistry/nwchem-json:latest',
                     'docker run -v $(pwd):/data openchemistry/nwchem-json:latest %s' % (
                input_name)],
        'input': [
            {
              'folderId': input_folder['_id'],
              'path': '.'
            }
        ],
        'output': [],
        'params': {
            'taskFlowId': task.taskflow.id
        }
    }

    client = create_girder_client(
                task.taskflow.girder_api_url, task.taskflow.girder_token)

    job = client.post('jobs', data=json.dumps(body))
    task.taskflow.set_metadata('jobs', [job])

    return job


def _create_job_nersc(task, cluster, input_file, input_folder):
    task.taskflow.logger.info('Create NWChem job.')

    body = {
        'name': 'nwchem_run',
        'commands': ['/usr/bin/srun -N 1  -n 32 %s %s' % (os.environ.get('OC_NWCHEM_PATH', 'nwchem'), input_file['name'])],
        'input': [
            {
              'folderId': input_folder['_id'],
              'path': '.'
            }
        ],
        'output': [],
        'params': {
            'taskFlowId': task.taskflow.id,
            'numberOfNodes': 1,
            'queue': 'debug',
            'constraint': 'haswell',
            'account': os.environ.get('OC_ACCOUNT')
        }
    }

    client = create_girder_client(
                task.taskflow.girder_api_url, task.taskflow.girder_token)

    job = client.post('jobs', data=json.dumps(body))
    task.taskflow.set_metadata('jobs', [job])

    return job

def _nersc(cluster):
    return cluster.get('name') in ['cori']

def _create_job(task, cluster, input_file, input_folder):
    if _nersc(cluster):
        return _create_job_nersc(task, cluster, input_file, input_folder)
    else:
        return _create_job_ec2(task, cluster, input_file, input_folder)

@cumulus.taskflow.task
def submit(task, input_, cluster, run_folder, input_file, input_folder):
    job = _create_job(task, cluster, input_file, input_folder)

    girder_token = task.taskflow.girder_token
    task.taskflow.set_metadata('cluster', cluster)

    # Now download and submit job to the cluster
    task.taskflow.logger.info('Downloading input files to cluster.')
    download_job_input_folders(cluster, job,
                               girder_token=girder_token, submit=False)
    task.taskflow.logger.info('Downloading complete.')

    task.taskflow.logger.info('Submitting job %s to cluster.' % job['_id'])
    girder_token = task.taskflow.girder_token

    try:
        submit_job(cluster, job, girder_token=girder_token, monitor=False)
    except:
        import traceback
        traceback.print_exc()

    monitor_job.apply_async((cluster, job), {'girder_token': girder_token,
                                             'monitor_interval': 10},
                            link=postprocess.s(run_folder, input_, cluster, job))

@cumulus.taskflow.task
def postprocess(task, _, run_folder, input_, cluster, job):
    task.taskflow.logger.info('Uploading results from cluster')

    client = create_girder_client(
        task.taskflow.girder_api_url, task.taskflow.girder_token)

    output_folder = client.createFolder(run_folder['_id'],
                                       'output')
    # Refresh state of job
    job = client.get('jobs/%s' % job['_id'])
    job['output'] = [{
        'folderId': output_folder['_id'],
        'path': '.'
    }]

    upload_job_output_to_folder(cluster, job, girder_token=task.taskflow.girder_token)

    task.taskflow.logger.info('Upload job output complete.')

    input_file_name = task.taskflow.get_metadata('inputFileName')
    input_file_name
    # Call to ingest the files
    for item in client.listItem(output_folder['_id']):
        if item['name'].endswith('.json'):
            files = list(client.listFile(item['_id']))
            if len(files) != 1:
                raise Exception('Expecting a single file under item, found: %s' + len(files))

            json_output_file_id = files[0]['_id']
            # Now call endpoint to ingest result
            body = {
                'calculationId': input_['calculation']['_id'],
                'fileId': json_output_file_id,
                'public': True
            }

            client.post('molecules', json=body)
