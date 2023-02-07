import base64
import concurrent
import concurrent.futures
import io
import json
import os
import pickle
import sys
import time
import uuid
from datetime import date
from typing import Dict, List, Optional, Union
from uuid import UUID

import numpy as np
import pyarrow as pa
import requests
from loguru import logger
from pyarrow import compute as pc
from pydantic import BaseModel, Field
from tqdm import tqdm

import nomic

from .cli import refresh_bearer_token, validate_api_http_response
from .settings import *
from .utils import assert_valid_project_id, get_object_size_in_bytes


class CreateIndexResponse(BaseModel):
    map: Optional[str] = Field(
        None, description="A link to the map this index creates. May take some time to be ready so check the job state."
    )
    job_id: str = Field(..., description="The job_id to track the progress of the index build.")
    index_id: str = Field(..., description="The unique identifier of the index being built.")
    project_id: str = Field(..., description="The id of the project this map is being created in")
    project_name: str = Field(..., description="The name of the project that was created.")


class AtlasUser:
    def __init__(self):
        self.credentials = refresh_bearer_token()


class AtlasClass(object):
    def __init__(self):
        '''
        Initializes the Atlas client.
        '''

        if self.credentials['tenant'] == 'staging':
            hostname = 'staging-api-atlas.nomic.ai'
        elif self.credentials['tenant'] == 'production':
            hostname = 'api-atlas.nomic.ai'
        else:
            raise ValueError("Invalid tenant.")

        self.atlas_api_path = f"https://{hostname}"
        token = self.credentials['token']
        self.token = token

        self.header = {"Authorization": f"Bearer {token}"}

        if self.token:
            response = requests.get(
                self.atlas_api_path + "/v1/user",
                headers=self.header,
            )
            response = validate_api_http_response(response)
            if not response.status_code == 200:
                logger.info("Your authorization token is no longer valid.")
        else:
            raise ValueError(
                "Could not find an authorization token. Run `nomic login` to authorize this client with the Nomic API."
            )

    @property
    def credentials(self):
        return refresh_bearer_token()

    def _get_current_user(self):
        response = requests.get(
            self.atlas_api_path + "/v1/user",
            headers=self.header,
        )
        response = validate_api_http_response(response)
        if not response.status_code == 200:
            raise ValueError("Your authorization token is no longer valid. Run `nomic login` to obtain a new one.")

        return response.json()

    def _validate_map_data_inputs(self, colorable_fields, id_field, data):
        '''Validates inputs to map data calls.'''

        if not isinstance(colorable_fields, list):
            raise ValueError("colorable_fields must be a list of fields")

        if id_field in colorable_fields:
            raise Exception(f'Cannot color by unique id field: {id_field}')

        for field in colorable_fields:
            if field not in data[0]:
                raise Exception(f"Cannot color by field `{field}` as it is not present in the meta-data.")

    def _get_current_users_main_organization(self):
        '''
        Retrieves the ID of the current users default organization.

        **Returns:** The ID of the current users default organization

        '''

        user = self._get_current_user()
        for organization in user['organizations']:
            if organization['user_id'] == user['sub'] and organization['access_role'] == 'OWNER':
                return organization

    def _delete_project_by_id(self, project_id):
        response = requests.post(
            self.atlas_api_path + "/v1/project/remove",
            headers=self.header,
            json={'project_id': project_id},
        )

    def _get_project_by_id(self, project_id: str):
        '''

        Args:
            project_id: The project id

        Returns:
            Returns the requested project.
        '''

        assert_valid_project_id(project_id)

        response = requests.get(
            self.atlas_api_path + f"/v1/project/{project_id}",
            headers=self.header,
        )

        if response.status_code != 200:
            raise Exception(f"Could not access project: {response.json()}")

        return response.json()

    def _get_index_job(self, job_id: str):
        '''

        Args:
            job_id: The job id to retrieve the state of.

        Returns:
            Job ID meta-data.
        '''

        response = requests.get(
            self.atlas_api_path + f"/v1/project/index/job/{job_id}",
            headers=self.header,
        )

        if response.status_code != 200:
            raise Exception(f'Could not access job state: {response.json()}')

        return response.json()

    def _validate_and_correct_user_supplied_metadata(
        self, data: List[Dict], project, replace_empty_string_values_with_string_null=True
    ):
        '''
        Validates the users metadata for Atlas compatability.

        1. If unique_id_field is specified, validates that each datum has that field. If not, adds it and then notifies the user that it was added.

        2. If a key is detected to store values that match an ISO8601 timestamp string ,Atlas will assume you are working with timestamps. If any additional metadata
        has this key associated with a non-ISO8601 timestamp string the upload will fail.

        Args:
            data: the user supplied list of data dictionaries
            project: the atlas project you are validating the data for.
            replace_empty_string_values_with_string_null: replaces empty string values with string nulls in all datums

        Returns:

        '''
        if not isinstance(data, list):
            raise Exception("Metadata must be a list of dictionaries")

        metadata_keys = None
        metadata_date_keys = []

        for datum in data:
            # The Atlas client adds a unique datum id field for each user.
            # It does not overwrite the field if it exists, instead map creation fails.
            if project.id_field in datum:
                if len(str(datum[project.id_field])) > 36:
                    raise ValueError(
                        f"{datum}\n The id_field `{datum[project.id_field]}` is greater than 36 characters. Atlas does not support id_fields longer than 36 characters."
                    )
            else:
                if project.id_field == ATLAS_DEFAULT_ID_FIELD:
                    datum[project.id_field] = str(uuid.uuid4())
                else:
                    raise ValueError(f"{datum} does not contain your specified id_field `{datum[project.id_field]}`")

            if not isinstance(datum, dict):
                raise Exception(
                    'Each metadata must be a dictionary with one level of keys and values of only string, int and float types.'
                )

            if metadata_keys is None:
                metadata_keys = sorted(list(datum.keys()))

                # figure out which are dates
                for key in metadata_keys:
                    try:
                        date.fromisoformat(str(datum[key]))
                        metadata_date_keys.append(key)
                    except ValueError:
                        pass

            datum_keylist = sorted(list(datum.keys()))
            if datum_keylist != metadata_keys:
                msg = 'All metadata must have the same keys, but found key sets: {} and {}'.format(
                    metadata_keys, datum_keylist
                )
                raise ValueError(msg)

            for key in datum:
                if key.startswith('_'):
                    raise ValueError('Metadata fields cannot start with _')

                if key in metadata_date_keys:
                    try:
                        date.fromisoformat(str(datum[key]))
                    except ValueError:
                        raise ValueError(
                            f"{datum} has timestamp key `{key}` which cannot be parsed as a ISO8601 string. See the following documentation in the Nomic client for working with timestamps: https://docs.nomic.ai/mapping_faq.html."
                        )

                if project.modality == 'text':
                    if isinstance(datum[key], str) and len(datum[key]) == 0:
                        if replace_empty_string_values_with_string_null:
                            datum[key] = 'null'
                        else:
                            msg = 'Datum {} had an empty string for key: {}'.format(datum, key)
                            raise ValueError(msg)

                if not isinstance(datum[key], (str, float, int)):
                    raise Exception(
                        f"Metadata sent to Atlas must be a flat dictionary. Values must be strings, floats or ints. Key `{key}` of datum {str(datum)} is in violation."
                    )

    def _get_existing_project_by_name(self, project_name, organization_name):
        '''
        Retrieves an existing project by name in a given organization
        Args:
            project_name: the project name
            organization_name: the organization name

        Returns:
            The project id

        '''
        if organization_name is None:
            organization = self._get_current_users_main_organization()
            organization_name = organization['nickname']
            organization_id = organization['organization_id']

        else:
            organization_id_request = requests.get(
                self.atlas_api_path + f"/v1/organization/search/{organization_name}", headers=self.header
            )
            if organization_id_request.status_code != 200:
                user = self._get_current_user()
                users_organizations = [org['nickname'] for org in user['organizations']]
                raise Exception(
                    f"No such organization exists: {organization_name}. You can add projects to the following organization: {users_organizations}"
                )
            organization_id = organization_id_request.json()['organization_id']

        # check if this project already exists.
        response = requests.post(
            self.atlas_api_path + "/v1/project/search/name",
            headers=self.header,
            json={'organization_name': organization_name, 'project_name': project_name},
        )
        if response.status_code != 200:
            raise Exception(f"Failed to create project: {response.json()}")
        search_results = response.json()['results']

        if search_results:
            existing_project = search_results[0]
            existing_project_id = existing_project['id']
            return {
                'project_id': existing_project_id,
                'organization_id': organization_id,
                'organization_name': organization_name,
            }

        return {'organization_id': organization_id, 'organization_name': organization_name}


class AtlasIndex(AtlasClass):
    """
    An AtlasIndex represents a single view of an Atlas Project at a point in time.

    An AtlasIndex typically contains one or more *projections* which are 2d representations of
    the points in the index that you can browse online.
    """

    pass


class AtlasProject(AtlasClass):
    def __init__(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        unique_id_field: Optional[str] = None,
        modality: Optional[str] = None,
        organization_name: Optional[str] = None,
        is_public: bool = True,
        project_id=None,
        reset_project_if_exists=False,
        add_datums_if_exists=True,
    ):

        """
        Creates or loads an Atlas project.
        Atlas projects store data (text, embeddings, etc) that you can organize by building indices.
        If the organization already contains a project with this name, it will be returned instead.

        **Parameters:**

        * **project_name** - The name of the project.
        * **description** - A description for the project.
        * **unique_id_field** - The field that uniquely identifies each datum. If a datum does not contain this field, it will be added and assigned a random unique ID.
        * **modality** - The data modality of this project. Currently, Atlas supports either `text` or `embedding` modality projects.
        * **organization_name** - The name of the organization to create this project under. You must be a member of the organization with appropriate permissions. If not specified, defaults to your user account's default organization.
        * **is_public** - Should this project be publicly accessible for viewing (read only). If False, only members of your Nomic organization can view.
        * **reset_project_if_exists** - If the requested project exists in your organization, will delete it and re-create it.
        * **project_id** - An alternative way to retrieve a project is by passing the project_id directly. This only works if a project exists.
        * **reset_project_if_exists** - If the specified project exists in your organization, reset it by deleting all of its data. This means your uploaded data will not be contextualized with existing data.
        * **add_datums_if_exists** - If specifying an existing project and you want to add data to it, set this to true.
        **Returns:** project_id on success.

        """
        assert name is not None or project_id is not None, "You must pass a name or project_id"

        super().__init__()

        if project_id is not None:
            self.meta = self._get_project_by_id(project_id)
            return self

        project_id = self.create_project(
            project_name=name,
            description=description,
            unique_id_field=unique_id_field,
            modality=modality,
            organization_name=organization_name,
            is_public=is_public,
            reset_project_if_exists=reset_project_if_exists,
            add_datums_if_exists=add_datums_if_exists,
        )

        self.meta = self._get_project_by_id(project_id=project_id)

    def delete(self):
        '''
        Deletes an atlas project with all associated metadata.

        **Parameters:**

        * **project_id** - The id of the project you want to delete.
        '''
        organization = self._get_current_users_main_organization()
        organization_name = organization['nickname']

        logger.info(f"Deleting project `{self.name}` from organization `{organization_name}`")

        self._delete_project_by_id(project_id=self.id)

        return False

    def create_project(
        self,
        project_name: str,
        description: str,
        unique_id_field: str,
        modality: str,
        organization_name: Optional[str] = None,
        is_public: bool = True,
        reset_project_if_exists: bool = False,
        add_datums_if_exists: bool = False,
    ):
        '''
        Creates an Atlas project.
        Atlas projects store data (text, embeddings, etc) that you can organize by building indices.
        If the organization already contains a project with this name, it will be returned instead.

        **Parameters:**

        * **project_name** - The name of the project.
        * **description** - A description for the project.
        * **unique_id_field** - The field that uniquely identifies each datum. If a datum does not contain this field, it will be added and assigned a random unique ID.
        * **modality** - The data modality of this project. Currently, Atlas supports either `text` or `embedding` modality projects.
        * **organization_name** - The name of the organization to create this project under. You must be a member of the organization with appropriate permissions. If not specified, defaults to your user accounts default organization.
        * **is_public** - Should this project be publicly accessible for viewing (read only). If False, only members of your Nomic organization can view.
        * **reset_project_if_exists** - If the requested project exists in your organization, will delete it and re-create it.
        * **add_datums_if_exists** - Add datums if the project already exists

        **Returns:** project_id on success.

        '''
        results = self._get_existing_project_by_name(project_name=project_name, organization_name=organization_name)
        organization_name = results['organization_name']
        organization_id = results['organization_id']

        if 'project_id' in results:
            project_id = results['project_id']
            if reset_project_if_exists:
                logger.info(
                    f"Found existing project `{project_name}` in organization `{organization_name}`. Clearing it of data by request."
                )
                self._delete_project_by_id(project_id=project_id)
            else:
                if add_datums_if_exists:
                    logger.info(
                        f"Found existing project `{project_name}` in organization `{organization_name}`. Adding data to this project instead of creating a new one."
                    )
                    return project_id
                else:
                    raise ValueError(
                        f"Project already exists with the name `{project_name}` in organization `{organization_name}`."
                        f"You can add datums to it by settings `add_datums_if_exists = True` or reset it by specifying `reset_project_if_exist=True` on a new upload."
                    )

        supported_modalities = ['text', 'embedding']
        if modality not in supported_modalities:
            msg = 'Tried to create project with modality: {}, but Atlas only supports: {}'.format(
                modality, supported_modalities
            )
            raise ValueError(msg)
        logger.info(f"Creating project `{project_name}` in organization `{organization_name}`")

        response = requests.post(
            self.atlas_api_path + "/v1/project/create",
            headers=self.header,
            json={
                'organization_id': organization_id,
                'project_name': project_name,
                'description': description,
                'unique_id_field': unique_id_field,
                'modality': modality,
                'is_public': is_public,
            },
        )
        if response.status_code != 201:
            raise Exception(f"Failed to create project: {response.json()}")
        return response.json()['project_id']

    def project_info(self):
        return self._latest_project_state().meta

    def _latest_project_state(self):
        response = requests.get(
            self.atlas_api_path + f"/v1/project/{self.id}",
            headers=self.header,
        )
        self.meta = response.json()
        return self

    @property
    def indices(self):
        return self.project_info()['atlas_indices']

    @property
    def projections(self):
        vs = []
        for index in self.indices:
            for projection in index['projections']:
                vs.append(nomic.AtlasProjection(self, projection['id']))
        return vs

    @property
    def id(self):
        return self.meta['id']

    @property
    def id_field(self):
        return self.meta['unique_id_field']

    @property
    def total_datums(self):
        return self.meta['total_datums_in_project']

    @property
    def modality(self):
        return self.meta['modality']

    @property
    def name(self):
        return self.meta['project_name']

    @property
    def description(self):
        return self.meta['description']

    @property
    def project_fields(self):
        return self.meta['project_fields']

    @property
    def is_locked(self):
        return self.meta['insert_update_delete_lock']

    @property
    def is_accepting_data(self):
        '''
        Checks if the project can accept data. Projects cannot accept data when they are being indexed.

        **Parameters:**

        **Returns:** True if project is unlocked for data additions, false otherwise.
        '''
        self._latest_project_state()
        return not self.is_locked

    def create_index(
        self,
        index_name: str,
        indexed_field=None,
        colorable_fields: list = [],
        multilingual: bool = False,
        build_topic_model: bool = False,
        projection_n_neighbors=DEFAULT_PROJECTION_N_NEIGHBORS,
        projection_epochs=DEFAULT_PROJECTION_EPOCHS,
        projection_spread=DEFAULT_PROJECTION_SPREAD,
        topic_label_field=None,
        reuse_lm_from=None,
    ) -> CreateIndexResponse:
        '''
        Creates an index in the specified project

        **Parameters:**

        * **project_id** - The ID of the project this index is being built under.
        * **index_name** - The name of the index
        * **indexed_field** - Default None. For text projects, name the data field corresponding to the text to be mapped.
        * **colorable_fields** - The project fields you want to be able to color by on the map. Must be a subset of the projects fields.
        * **multilingual** - Should the map take language into account? If true, points from different languages but semantically similar text are close together.
        * **shard_size** - Embeddings are uploaded in parallel by many threads. Adjust the number of embeddings to upload by each worker.
        * **num_workers** - The number of worker threads to upload embeddings with.
        * **topic_label_field** - A text field to estimate topic labels from.
        * **reuse_lm_from** - An optional index id from the same project whose atoms and embeddings should be reused. Text projects only.

        **Returns:** A link to your map.
        '''
        self._latest_project_state()

        #for large projects, alter the default projection configurations.
        if self.total_datums >= 1_000_000:
            if projection_epochs == DEFAULT_PROJECTION_EPOCHS and projection_n_neighbors == DEFAULT_PROJECTION_N_NEIGHBORS:
                projection_n_neighbors = DEFAULT_LARGE_PROJECTION_N_NEIGHBORS
                projection_epochs = DEFAULT_LARGE_PROJECTION_EPOCHS

        if self.modality == 'embedding':
            build_template = {
                'project_id': self.id,
                'index_name': index_name,
                'indexed_field': None,
                'atomizer_strategies': None,
                'model': None,
                'colorable_fields': colorable_fields,
                'model_hyperparameters': None,
                'nearest_neighbor_index': 'HNSWIndex',
                'nearest_neighbor_index_hyperparameters': json.dumps({'space': 'l2', 'ef_construction': 100, 'M': 16}),
                'projection': 'NomicProject',
                'projection_hyperparameters': json.dumps(
                    {'n_neighbors': projection_n_neighbors, 'n_epochs': projection_epochs, 'spread': projection_spread}
                ),
                'topic_model_hyperparameters': json.dumps(
                    {'build_topic_model': build_topic_model, 'community_description_target_field': topic_label_field}
                ),
            }

        elif self.modality == 'text':
            if indexed_field is None:
                raise Exception("You did not specify a field to index. Specify an 'indexed_field'.")

            if indexed_field not in self.project_fields:
                raise Exception(f"Your index field is not valid. Valid options are: {self.project_fields}")

            model = 'NomicEmbed'
            if multilingual:
                model = 'NomicEmbedMultilingual'

            build_template = {
                'project_id': self.id,
                'index_name': index_name,
                'indexed_field': indexed_field,
                'atomizer_strategies': ['document', 'charchunk'],
                'model': model,
                'colorable_fields': colorable_fields,
                'reuse_atoms_and_embeddings_from': reuse_lm_from,
                'model_hyperparameters': json.dumps(
                    {
                        'dataset_buffer_size': 1000,
                        'batch_size': 20,
                        'polymerize_by': 'charchunk',
                        'norm': 'both',
                    }
                ),
                'nearest_neighbor_index': 'HNSWIndex',
                'nearest_neighbor_index_hyperparameters': json.dumps({'space': 'l2', 'ef_construction': 100, 'M': 16}),
                'projection': 'NomicProject',
                'projection_hyperparameters': json.dumps(
                    {'n_neighbors': projection_n_neighbors, 'n_epochs': projection_epochs, 'spread': projection_spread}
                ),
                'topic_model_hyperparameters': json.dumps(
                    {'build_topic_model': build_topic_model, 'community_description_target_field': indexed_field}
                ),
            }

        response = requests.post(
            self.atlas_api_path + "/v1/project/index/create",
            headers=self.header,
            json=build_template,
        )
        if response.status_code != 200:
            logger.info('Create project failed with code: {}'.format(response.status_code))
            logger.info('Additional info: {}'.format(response.json()))
            raise Exception(response.json['detail'])

        job_id = response.json()['job_id']

        job = requests.get(
            self.atlas_api_path + f"/v1/project/index/job/{job_id}",
            headers=self.header,
        ).json()

        index_id = job['index_id']

        def get_projection_id():
            self._latest_project_state()

            projection_id = None
            for index in self.indices:
                if index['id'] == index_id:
                    projection_id = index['projections'][0]['id']
                    break
            return projection_id

        projection_id = get_projection_id()

        if not projection_id:
            time.sleep(5)
            projection_id = get_projection_id()

        to_return = {'job_id': job_id, 'index_id': index_id}
        if not projection_id:
            logger.warning(
                "Could not find a map being built for this project. See atlas.nomic.ai/dashboard for map status."
            )
        else:
            if self.credentials['tenant'] == 'staging':
                to_return['map'] = f"https://staging-atlas.nomic.ai/map/{self.id}/{projection_id}"
            else:
                to_return['map'] = f"https://atlas.nomic.ai/map/{self.id}/{projection_id}"
            logger.info(f"Created map `{index_name}` in project `{self.name}`: {to_return['map']}")
        to_return['project_id'] = self.id
        to_return['project_name'] = self.name
        return CreateIndexResponse(**to_return)

    def __repr__(self):
        m = self.meta
        return f"Nomic project: <{m}>"

    def add_text(
        self,
        data: List[Dict],
        shard_size=1000,
        num_workers=10,
        replace_empty_string_values_with_string_null=True,
        pbar=None,
    ):
        '''
        Adds data to a text project.

        **Parameters:**

        * **data** - An [N,] element list of dictionaries containing metadata for each embedding.
        * **shard_size** - Embeddings are uploaded in parallel by many threads. Adjust the number of embeddings to upload by each worker.
        * **num_workers** - The number of worker threads to upload embeddings with.
        * **replace_empty_string_values_with_string_null** - Replaces empty values in metadata with null. If false, will fail if empty values are supplied.

        **Returns:** True on success.
        '''

        # Each worker currently is to slow beyond a shard_size of 5000
        shard_size = min(shard_size, 5000)

        # Check if this is a progressive project
        response = requests.get(
            self.atlas_api_path + f"/v1/project/{self.id}",
            headers=self.header,
        )

        project = response.json()
        if project['modality'] != 'text':
            msg = 'Cannot add text to project with modality: {}'.format(self.modality)
            raise ValueError(msg)

        progressive = len(self.indices) > 0

        if project['insert_update_delete_lock']:
            raise Exception("Project is currently indexing and cannot ingest new datums. Try again later.")

        try:
            self._validate_and_correct_user_supplied_metadata(
                data=data,
                project=project,
                replace_empty_string_values_with_string_null=replace_empty_string_values_with_string_null,
            )
        except BaseException as e:
            raise e

        upload_endpoint = "/v1/project/data/add/json/initial"
        if progressive:
            upload_endpoint = "/v1/project/data/add/json/progressive"

        # Actually do the upload
        def send_request(i):
            data_shard = data[i : i + shard_size]
            if get_object_size_in_bytes(data_shard) > 8000000:
                raise Exception(
                    "Your metadata upload shards are to large. Try decreasing the shard size or removing un-needed fields from the metadata."
                )
            response = requests.post(
                self.atlas_api_path + upload_endpoint,
                headers=self.header,
                json={'project_id': self.id, 'data': data_shard},
            )
            return response

        # if this method is being called internally, we pass a global progress bar
        close_pbar = False
        if pbar is None:
            logger.info("Uploading text to Atlas.")
            close_pbar = True
            pbar = tqdm(total=int(len(data)) // shard_size)
        failed = 0
        succeeded = 0
        errors_504 = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(send_request, i): i for i in range(0, len(data), shard_size)}

            while futures:
                # check for status of the futures which are currently working
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                # process any completed futures
                for future in done:
                    response = future.result()
                    if response.status_code != 200:
                        try:
                            logger.error(f"Shard upload failed: {response.json()}")
                            if 'more datums exceeds your organization limit' in response.json():
                                return False
                            if 'Project transaction lock is held' in response.json():
                                raise Exception(
                                    "Project is currently indexing and cannot ingest new datums. Try again later."
                                )
                            if 'Insert failed due to ID conflict' in response.json():
                                continue
                        except (requests.JSONDecodeError, json.decoder.JSONDecodeError):
                            if response.status_code == 413:
                                # Possibly split in two and retry?
                                logger.error("Shard upload failed: you are sending meta-data that is too large.")
                                pbar.update(1)
                                response.close()
                                failed += shard_size
                            elif response.status_code == 504:
                                errors_504 += shard_size
                                start_point = futures[future]
                                logger.debug(
                                    f"Connection failed for records {start_point}-{start_point + shard_size}, retrying."
                                )
                                failure_fraction = errors_504 / (failed + succeeded + errors_504)
                                if failure_fraction > 0.25 and errors_504 > shard_size * 3:
                                    raise RuntimeError(
                                        "Atlas is under high load and cannot ingest datums at this time. Please try again later."
                                    )
                                new_submission = executor.submit(send_request, start_point)
                                futures[new_submission] = start_point
                                response.close()
                            else:
                                logger.error(f"Shard upload failed: {response}")
                                failed += shard_size
                                pbar.update(1)
                                response.close()
                    else:
                        # A successful upload.
                        succeeded += shard_size
                        pbar.update(1)
                        response.close()

                    # remove the now completed future
                    del futures[future]

        # close the progress bar if this method was called with no external progresbar
        if close_pbar:
            pbar.close()

        if failed:
            logger.warning(f"Failed to upload {len(failed) * shard_size} datums")
        if close_pbar:
            if failed:
                logger.warning("Text upload partially succeeded.")
            else:
                logger.info("Text upload succeeded.")

        return True

    def add_embeddings(
        self,
        embeddings: np.array,
        data: List[Dict],
        shard_size=1000,
        num_workers=10,
        replace_empty_string_values_with_string_null=True,
        pbar=None,
    ):
        '''
        Adds embeddings to an embedding project. Pair each embedding with meta-data to explore your embeddings.

        **Parameters:**

        * **embeddings** - An [N,d] numpy array containing the batch of N embeddings to add.
        * **data** - An [N,] element list of dictionaries containing metadata for each embedding.
        * **shard_size** - Embeddings are uploaded in parallel by many threads. Adjust the number of embeddings to upload by each worker.
        * **num_workers** - The number of worker threads to upload embeddings with.
        * **replace_empty_string_values_with_string_null** - Replaces empty values in metadata with null. If false, will fail if empty values are supplied.

        **Returns:** True on success.
        '''

        # Each worker currently is to slow beyond a shard_size of 5000
        shard_size = min(shard_size, 5000)

        # Check if this is a progressive project

        if self.modality != 'embedding':
            msg = 'Cannot add embedding to project with modality: {}'.format(self.modality)
            raise ValueError(msg)

        if self.is_locked:
            raise Exception("Project is currently indexing and cannot ingest new datums. Try again later.")

        progressive = len(self.indices) > 0
        try:
            self._validate_and_correct_user_supplied_metadata(
                data=data,
                project=self,
                replace_empty_string_values_with_string_null=replace_empty_string_values_with_string_null,
            )
        except BaseException as e:
            raise e

        upload_endpoint = "/v1/project/data/add/embedding/initial"
        if progressive:
            upload_endpoint = "/v1/project/data/add/embedding/progressive"

        # Actually do the upload
        def send_request(i):
            data_shard = data[i : i + shard_size]

            if get_object_size_in_bytes(data_shard) > 8000000:
                raise Exception(
                    "Your metadata upload shards are to large. Try decreasing the shard size or removing un-needed fields from the metadata."
                )
            embedding_shard = embeddings[i : i + shard_size, :]

            bytesio = io.BytesIO()
            np.save(bytesio, embedding_shard)
            response = requests.post(
                self.atlas_api_path + upload_endpoint,
                headers=self.header,
                json={
                    'project_id': self.id,
                    'embeddings': base64.b64encode(bytesio.getvalue()).decode('utf-8'),
                    'data': data_shard,
                },
            )
            return response

        # if this method is being called internally, we pass a global progress bar
        close_pbar = False
        if pbar is None:
            logger.info("Uploading embeddings to Atlas.")
            close_pbar = True
            pbar = tqdm(total=int(embeddings.shape[0]) // shard_size)
        failed = 0
        succeeded = 0
        errors_504 = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(send_request, i): i for i in range(0, len(data), shard_size)}

            while futures:
                # check for status of the futures which are currently working
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                # process any completed futures
                for future in done:
                    response = future.result()
                    if response.status_code != 200:
                        try:
                            logger.error(f"Shard upload failed: {response.json()}")
                            if 'more datums exceeds your organization limit' in response.json():
                                return False
                            if 'Project transaction lock is held' in response.json():
                                raise Exception(
                                    "Project is currently indexing and cannot ingest new datums. Try again later."
                                )
                            if 'Insert failed due to ID conflict' in response.json():
                                continue
                        except (requests.JSONDecodeError, json.decoder.JSONDecodeError):
                            if response.status_code == 413:
                                # Possibly split in two and retry?
                                logger.error("Shard upload failed: you are sending meta-data that is too large.")
                                pbar.update(1)
                                response.close()
                                failed += shard_size
                            elif response.status_code == 504:
                                errors_504 += shard_size
                                start_point = futures[future]
                                logger.debug(
                                    f"Connection failed for records {start_point}-{start_point + shard_size}, retrying."
                                )
                                failure_fraction = errors_504 / (failed + succeeded + errors_504)
                                if failure_fraction > 0.25 and errors_504 > shard_size * 3:
                                    raise RuntimeError(
                                        "Atlas is under high load and cannot ingest datums at this time. Please try again later."
                                    )
                                new_submission = executor.submit(send_request, start_point)
                                futures[new_submission] = start_point
                                response.close()
                            else:
                                logger.error(f"Shard upload failed: {response}")
                                failed += shard_size
                                pbar.update(1)
                                response.close()
                    else:
                        # A successful upload.
                        succeeded += shard_size
                        pbar.update(1)
                        response.close()

                    # remove the now completed future
                    del futures[future]

        # close the progress bar if this method was called with no external progresbar
        if close_pbar:
            pbar.close()

        if failed:
            logger.warning(f"Failed to upload {failed} datums")
        if close_pbar:
            if failed:
                logger.warning("Embedding upload partially succeeded.")
            else:
                logger.info("Embedding upload succeeded.")

        return True

    def refresh_maps(self):
        '''
        Refresh all maps in a project with the latest state.

        **Parameters:**

        * **project_id** - The id of the project whose maps will be refreshed.

        **Returns:** a list of jobs
        '''

        response = requests.post(
            self.atlas_api_path + "/v1/project/update_indices",
            headers=self.header,
            json={
                'project_id': self.id,
            },
        )

        logger.info(f"Updating maps in project `{self.name}`")

        return self

    def download_embeddings(self, atlas_index_id, output_dir, num_workers=10):
        '''
        Downloads a mapping from datum_id to embedding in shards to the provided directory

        Args:
            atlas_index_id: the id of the index whose ambient embeddings you want
            output_dir: the directory to save shards to
        '''
        self._latest_project_state()

        total_datums = self.total_datums
        if self.is_locked:
            raise Exception('Project is locked! Please wait until the project is unlocked to download embeddings')

        offset = 0
        limit = EMBEDDING_PAGINATION_LIMIT

        def download_shard(offset, check_access=False):
            response = requests.get(
                self.atlas_api_path + f"/v1/project/data/get/embedding/{self.id}/{atlas_index_id}/{offset}/{limit}",
                headers=self.header,
            )

            if response.status_code != 200:
                raise Exception(response.json())

            if check_access:
                return
            try:
                content = response.json()

                shard_name = '{}_{}_{}.pkl'.format(atlas_index_id, offset, offset + limit)
                shard_path = os.path.join(output_dir, shard_name)
                with open(shard_path, 'wb') as f:
                    pickle.dump(content, f)

            except Exception as e:
                logger.error('Shard {} download failed with error: {}'.format(shard_name, e))

        download_shard(0, check_access=True)

        with tqdm(total=total_datums // limit) as pbar:
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {
                    executor.submit(download_shard, cur_offset): cur_offset
                    for cur_offset in range(0, total_datums, limit)
                }
                for future in concurrent.futures.as_completed(futures):
                    _ = future.result()
                    pbar.update(1)

    def get_embedding_iterator(self, atlas_index_id):
        '''
        Returns an iterable of datum_ids and embeddings from the given index

        Args:
            atlas_index_id: the id of the index whose ambient embeddings you want

        Returns:
            Iterable[Tuple[datum_ids, embeddings]]
        '''

        if self.is_locked:
            raise Exception('Project is locked! Please wait until the project is unlocked to download embeddings')

        offset = 0
        limit = EMBEDDING_PAGINATION_LIMIT
        while True:
            response = requests.get(
                self.atlas_api_path + f"/v1/project/data/get/embedding/{self.id}/{atlas_index_id}/{offset}/{limit}",
                headers=self.header,
            )
            if response.status_code != 200:
                raise Exception(response.json()['detail'])

            content = response.json()
            if len(content['datum_ids']) == 0:
                break
            offset += len(content['datum_ids'])

            yield content['datum_ids'], content['embeddings']

    def get_tags(self, index_name=None):
        '''
        Retrieves back all tags made in the web browser for a specific project and map.

        **Parameters:**

        * **index_name** - The name of the atlas index in the project.

        **Returns:** A dictionary mapping datum ids to tags.
        '''

        if len(self.indices) == 0:
            raise Exception(f'There are no indices in the project `{self.name}`.')

        target_index = None
        for index in self.indices:
            if index['index_name'] == index_name:
                target_index = index
                break
        if target_index is None:
            target_index = self.indices[0]

        # now get the tags
        datums_and_tags = requests.post(
            self.atlas_api_path + '/v1/project/tag/read/all_by_datum',
            headers=self.header,
            json={
                'project_id': self.id,
                'atlas_index_id': target_index['id'],
            },
        ).json()['results']

        datum_to_labels = {}
        for item in datums_and_tags:
            datum_to_labels[item['datum_id']] = item['labels']

        return datums_and_tags

    # def upload_embeddings(self, table: pa.Table, embedding_column: str = '_embedding'):
    #     """
    #     Uploads a single Arrow table to Atlas.
    #     """
    #     dimensions = table[embedding_column].type.list_size
    #     embs = pc.list_flatten(table[embedding_column]).to_numpy().reshape(-1, dimensions)
    #     self.atlas_client.add_embeddings(
    #         project_id=self.id, embeddings=embs, data=table.drop([embedding_column]).to_pylist(), shard_size=1500
    #     )