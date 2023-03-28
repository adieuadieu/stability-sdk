import grpc
import json
import logging
import os
import random
import shutil
import time
import uuid
import warnings

from google.protobuf.struct_pb2 import Struct
from PIL import Image
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from abc import ABC, abstractmethod

try:
    import cv2
    import numpy as np
except ImportError:
    warnings.warn(
        "Failed to import animation reqs. To use the animation toolchain, install the requisite dependencies via:"
        "   pip install --upgrade stability_sdk[anim]"
    )

import stability_sdk.interfaces.gooseai.dashboard.dashboard_pb2 as dashboard
import stability_sdk.interfaces.gooseai.dashboard.dashboard_pb2_grpc as dashboard_grpc
import stability_sdk.interfaces.gooseai.generation.generation_pb2 as generation
import stability_sdk.interfaces.gooseai.generation.generation_pb2_grpc as generation_grpc
import stability_sdk.interfaces.gooseai.project.project_pb2 as project
import stability_sdk.interfaces.gooseai.project.project_pb2_grpc as project_grpc

from .utils import (
    image_mix,
    image_to_png_bytes,
    image_to_prompt,
    tensor_to_prompt,
)


logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)


def open_channel(host: str, api_key: str = None, max_message_len: int = 10*1024*1024) -> grpc.Channel:
    options=[
        ('grpc.max_send_message_length', max_message_len),
        ('grpc.max_receive_message_length', max_message_len),
    ]
    if host.endswith(":443"):
        call_credentials = [grpc.access_token_call_credentials(api_key)]
        channel_credentials = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(), *call_credentials
        )
        channel = grpc.secure_channel(host, channel_credentials, options=options)
    else:
        channel = grpc.insecure_channel(host, options=options)
    return channel


class ClassifierException(Exception):
    """Raised when server classifies generated content as inappropriate."""
    def __init__(self, classifier_result: generation.ClassifierParameters):
        self.classifier_result = classifier_result

class OutOfCreditsException(Exception):
    """Raised when account doesn't have enough credits to perform a request."""
    def __init__(self, details: str):
        self.details = details


class Endpoint:
    def __init__(self, stub, engine_id):
        self.stub = stub
        self.engine_id = engine_id


class StorageBackend(ABC):
    def __init__(self, context: 'Context', primary: bool = False, primary_fs: bool = False):
        self._context = context
        self.primary = primary
        self.primary_fs = primary_fs

    @staticmethod
    @abstractmethod
    def create_project(
            context: 'Context',
            title: str,
            access: project.ProjectAccess = project.PROJECT_ACCESS_PRIVATE,
            status: project.ProjectStatus = project.PROJECT_STATUS_ACTIVE,
            proj_id_to_use: str = None
    ) -> 'Project':
        pass

    @staticmethod
    @abstractmethod
    def get_project(
                  context: 'Context',
                  id: str
    ) -> 'Project':
        pass

    @staticmethod
    @abstractmethod
    def delete_project(context: 'Context', id: str) -> None:
        pass

    @staticmethod
    @abstractmethod
    def list_projects(context: 'Context') -> List['Project']:
        pass

    @abstractmethod
    def get_project_settings(self, proj: 'Project', asset_id: str = None) -> dict:
        pass

    @abstractmethod
    def put_project_settings(self, context: 'Context', proj: 'Project', data: dict) -> str:
        pass

    def get_image_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> Image.Image:
        pass

    @abstractmethod
    def put_image_asset(self, proj: 'Project', image: Union[Image.Image, np.ndarray], use: generation.AssetUse, asset_id: str = None) -> str:
        pass

    def get_video_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> bytes:
        pass

    @abstractmethod
    def put_video_asset(self, proj: 'Project', video_path: str, asset_id: str) -> str:
        pass

    def update_project(context: 'Context', proj: 'Project', title: str = None, file_id: str = None, file_uri: str = None) -> None:
        pass


class AssetServiceBackend(StorageBackend):
    def __init__(self, context: 'Context', primary: bool = False):
        super().__init__(context, primary)

    @staticmethod
    def create_project(
            context: 'Context',
            title: str,
            access: project.ProjectAccess = project.PROJECT_ACCESS_PRIVATE,
            status: project.ProjectStatus = project.PROJECT_STATUS_ACTIVE,
            proj_id_to_use: str = None
    ) -> 'Project':
        req = project.CreateProjectRequest(title=title, access=access, status=status)
        proj: project.Project = context._proj_stub.Create(req, wait_for_ready=True)
        return Project(context, proj)

    @staticmethod
    def get_project(context: 'Context', id: str) -> 'Project':
        req = project.GetProjectRequest(id=id)
        proj: project.Project = context._proj_stub.Get(req, wait_for_ready=True)
        return Project(context, proj)

    @staticmethod
    def delete_project(context: 'Context', id: str) -> None:
        context._proj_stub.Delete(project.DeleteProjectRequest(id=id))

    @staticmethod
    def list_projects(context: 'Context') -> List['Project']:
        list_req = project.ListProjectRequest(owner_id="")
        results = []
        for proj in context._proj_stub.List(list_req, wait_for_ready=True):
            results.append(Project(context, proj))
        results.sort(key=lambda x: x.title.lower())
        return results

    def get_project_settings(self, proj: 'Project', asset_id: str = None) -> dict:
        asset_id = asset_id if asset_id else proj.file.id
        request = generation.Request(
            engine_id=self._context._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(
                    type=generation.ARTIFACT_TEXT,
                    mime="application/json",
                    uuid=asset_id,
                )
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_GET,
                project_id=proj.id,
                use=generation.ASSET_USE_PROJECT
            )
        )
        results = self._context._run_request(self._context._asset, request)
        if generation.ARTIFACT_TEXT in results:
            settings_json = json.loads(results[generation.ARTIFACT_TEXT][0])
            return settings_json
        raise Exception(f"Failed to load project file for {proj.id}")

    def put_project_settings(self, context: 'Context', proj: 'Project', data: dict) -> str:
        contents = json.dumps(data)
        request = generation.Request(
            engine_id=context._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(
                    type=generation.ARTIFACT_TEXT,
                    text=contents,
                    mime="application/json",
                    uuid=proj.file_id
                )
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_PUT,
                project_id=proj.id,
                use=generation.ASSET_USE_PROJECT
            )
        )
        results = context._run_request(context._asset, request)
        if generation.ARTIFACT_TEXT in results:
            return results[generation.ARTIFACT_TEXT][0]
        raise Exception(f"Failed to save project file for {proj.id}")

    def get_image_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> Image.Image:
        request = generation.Request(
            engine_id=self._context._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(type=generation.ARTIFACT_IMAGE, mime="image/png", uuid=asset_id)
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_GET,
                project_id=proj.id,
                use=use
            )
        )
        results = self._context._run_request(self._context._asset, request)
        if generation.ARTIFACT_IMAGE in results:
            img = results[generation.ARTIFACT_IMAGE][0]
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            return pil_img
        raise Exception(f"Failed to load image asset {asset_id} for project {proj.id}")

    def put_image_asset(self, proj: 'Project', image: Union[Image.Image, np.ndarray], use: generation.AssetUse, asset_id: str = None) -> str:
        request = generation.Request(
            engine_id=self._context._asset.engine_id,
            prompt=[image_to_prompt(image)],
            asset=generation.AssetParameters(
                action=generation.ASSET_PUT,
                project_id=proj.id,
                use=use
            )
        )
        results = self._context._run_request(self._context._asset, request)
        if generation.ARTIFACT_TEXT in results:
            return results[generation.ARTIFACT_TEXT][0]
        raise Exception(f"Failed to store image asset for project {proj.id}")

    def get_video_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> str:
        request = generation.Request(
            engine_id=self._context._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(type=generation.ARTIFACT_VIDEO, mime="video/mp4", uuid=asset_id)
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_GET,
                project_id=proj.id,
                use=use
            )
        )
        results = self._context._run_request(self._context._asset, request)
        # TODO: In testing so far, results contains ARTIFACT_VIDEO key.. but the value for it is an empty list.
        # Thus it doesn't seem to be working.
        if generation.ARTIFACT_VIDEO in results:
            return results[generation.ARTIFACT_VIDEO][0]
        raise Exception(f"Failed to load video asset {asset_id} for project {proj.id}")

    def put_video_asset(self, proj: 'Project', video_path: str, asset_id: str) -> str:
        if not os.path.isfile(video_path) or not video_path.endswith(".mp4"):
            raise ValueError("Invalid video file path. Must be an existing .mp4 file.")

        with open(video_path, "rb") as f:
            binary_data = f.read()

        request = generation.Request(
            engine_id=self._context._asset.engine_id,
            prompt=[
                generation.Prompt(
                    artifact=generation.Artifact(
                        type=generation.ARTIFACT_VIDEO,
                        mime="video/mp4",
                        binary=binary_data,
                    )
                )
            ],
            asset=generation.AssetParameters(
                action=generation.ASSET_PUT,
                project_id=proj.id,
                use=generation.ASSET_USE_INPUT,
            ),
        )
        results = self._context._run_request(self._context._asset, request)
        if generation.ARTIFACT_TEXT in results:
            return results[generation.ARTIFACT_TEXT][0]
        raise Exception(f"Failed to store video asset for project {proj.id}")

    def update_project(context: 'Context', proj: 'Project', title: str = None, file_id: str = None, file_uri: str = None) -> None:
        file = project.ProjectAsset(
            id=file_id,
            uri=file_uri,
            use=project.PROJECT_ASSET_USE_PROJECT,
        ) if file_id and file_uri else None

        context._proj_stub.Update(project.UpdateProjectRequest(
            id=proj.id,
            title=title,
            file=file
        ))


class LocalFileBackend(StorageBackend):
    _projects_root = None

    def __init__(self, context: 'Context', primary: bool = False, primary_fs: bool = True, projects_root = 'projects'):
        super().__init__(context, primary, primary_fs = primary_fs)
        LocalFileBackend._projects_root = projects_root

    @staticmethod
    def create_project(
            context: 'Context',
            title: str,
            access: project.ProjectAccess = project.PROJECT_ACCESS_PRIVATE,
            status: project.ProjectStatus = project.PROJECT_STATUS_ACTIVE,
            proj_id_to_use: str = None
    ) -> 'Project':
        proj_id = proj_id_to_use if proj_id_to_use else str(uuid.uuid4())
        proj_file_id = proj_id # Let's keep it the same as the proj_id for now
        proj = {"id": proj_id,
                "title": title,
                "file": {"id": proj_file_id}}
        output_path = os.path.join(LocalFileBackend._projects_root, proj_id, proj_file_id)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as file:
            json.dump(proj, file)
        return Project(context, proj)

    @staticmethod
    def get_project(context: 'Context', id: str) -> 'Project':
        input_path = os.path.join(LocalFileBackend._projects_root, id, id)
        with open(input_path, "r") as file:
            proj = json.load(file)
        return Project(context, proj)

    @staticmethod
    def delete_project(context: 'Context', id: str):
        if not id:
            return
        project_dir_path = os.path.join(LocalFileBackend._projects_root, id)
        shutil.rmtree(project_dir_path)

    @staticmethod
    def list_projects(context: 'Context') -> List['Project']:
        # This returns a listing of directories in the projects root.
        proj_root = LocalFileBackend._projects_root
        all_entries = os.listdir(proj_root)
        directories = [entry for entry in all_entries if os.path.isdir(os.path.join(proj_root, entry))]
        projects = []
        for proj_id in directories:
            proj_path = LocalFileBackend.get_path_for_asset(proj_id, proj_id)
            try:
                with open(proj_path, "r") as file:
                    proj_json = json.load(file)
                    proj_data = {"id": proj_json["id"],
                                 "title": proj_json["title"],
                                 "file": {"id": proj_json["file"]["id"]}}
                    projects.append(Project(context, proj_data))
            except FileNotFoundError:
                pass
        return projects

    def get_project_settings(self, proj: 'Project', asset_id: str = None) -> dict:
        input_path = self.get_path_for_asset(proj.id, "project_settings.json")
        with open(input_path, "r") as file:
            settings_json = json.load(file)
        return settings_json

    def put_project_settings(self, context: 'Context', proj: 'Project', data: dict) -> str:
        filename = "project_settings.json"
        output_path = self.get_path_for_asset(proj.id, filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as file:
            json.dump(data, file)
        return filename

    def get_image_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> Image.Image:
        input_path = self.get_path_for_asset(proj.id, asset_id + '.png')
        pil_image = Image.open(input_path)
        return pil_image

    def put_image_asset(self, proj: 'Project',
                        image: Union[Image.Image, np.ndarray],
                        use: generation.AssetUse,
                        asset_id: str = None) -> str:
        png = image_to_png_bytes(image)
        if asset_id is not None:
            filename = asset_id
        else:
            if not self.primary:
                raise ValueError("If asset_id is None, then LocalFileBackend must be primary.")
            filename = str(uuid.uuid4())
        output_path = self.get_path_for_asset(proj.id, filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path + '.png', "wb") as file:
            file.write(png)
        return filename

    def get_video_asset(self, proj: 'Project', asset_id: str, use: generation.AssetUse) -> bytes:
        input_path = self.get_path_for_asset(proj.id, asset_id)
        with open(input_path, 'rb') as file:
            binary_data = file.read()
        return binary_data

    def put_video_asset(self, proj: 'Project', video_path: str, asset_id: str = None) -> str:
        if not os.path.isfile(video_path) or not video_path.endswith(".mp4"):
            raise ValueError("Invalid video file path. Must be an existing .mp4 file.")

        if asset_id is not None:
            filename = asset_id
        else:
            if not self.primary:
                raise ValueError("If name is None, then LocalFileBackend must be primary.")
            filename = str(uuid.uuid4())
        output_path = self.get_path_for_asset(proj.id, filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy(video_path, output_path)
        return filename

    @staticmethod
    def get_path_for_asset(project_id: str, filename: str):
        path = os.path.join(LocalFileBackend._projects_root, project_id, filename)
        return path

    def update_project(context: 'Context', proj: 'Project', title: str = None, file_id: str = None, file_uri: str = None):
        proj_file_id = proj.file.id
        proj = {"id": proj.id,
                "title": title if title is not None else proj.title,
                "file": {"id": file_id if file_id is not None else proj_file_id}}
        output_path = os.path.join(LocalFileBackend._projects_root, proj.id, proj_file_id)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as file:
            json.dump(proj, file)
        return Project(context, proj)


class Project():
    _backends = None
    _metadata_index = None

    def __init__(self, context: 'Context', proj: Union[project.Project, dict]):
        ## __init__ could take backends: Optional[List[StorageBackend]] = None
        # self._backends = backends if backends else [AssetServiceBackend(primary=True)]
        self._context = context

        # proj should be project.Project or dict
        # Currently, a supplied project.Project may contain additional properties that are ignored.
        if isinstance(proj, dict):
            self._project = project.Project()
            self._project.id = proj["id"]
            self._project.title = proj["title"]
            self._project.file.id = proj["file"]["id"]
        else:
            self._project = proj

    def _primary_backend(self) -> Optional[StorageBackend]:
        for backend in self.backends:
            if backend.primary:
                return backend
        return None

    @property
    def backends(self) -> str:
        return Project._backends

    @property
    def id(self) -> str:
        return self._project.id

    @property
    def file_id(self) -> str:
        return self._project.file.id

    @property
    def title(self) -> str:
        return self._project.title

    @classmethod
    def init_backends(cls, context: 'Context'):
        cls._backends = [
            AssetServiceBackend(context=context, primary=True),
            LocalFileBackend(context=context, primary=False)]
        #cls._backends = [LocalFileBackend(context=context, primary=True)]
        cls._metadata_index = cls.load_metadata_index()

    @staticmethod
    def create(
            context: 'Context',
            title: str,
            access: project.ProjectAccess = project.PROJECT_ACCESS_PRIVATE,
            status: project.ProjectStatus = project.PROJECT_STATUS_ACTIVE
    ) -> 'Project':
        asset_id = None
        for backend in Project._backends:
            proj = backend.create_project(context, title, access, status, asset_id)
            if isinstance(proj, dict):
                proj_id = proj["id"]
                proj_title = proj["title"]
            else:
                proj_id = proj.id
                proj_title = proj.title
            if backend.primary:
                asset_id = proj_id
            if backend.primary_fs:
                filename = proj_id
                proj_file_id = proj.file_id
        mimetype = "application/json"
        Project.add_asset_metadata(proj_id, asset_id, mimetype, filename, project_key="project_file_id")
        return proj

    @classmethod
    def get(cls,
            context: 'Context',
            id: str
    ) -> 'Project':
        for backend in cls._backends:
            if backend.primary:
                proj = backend.get_project(context, id)
                return proj
        raise Exception(f"Failed to list projects")

    def list_assets(self):
        req = project.QueryAssetsRequest(id=self.id)
        query_assets_response: project.QueryAssetsResponse = self._context._proj_stub.QueryAssets(req,
                                                                                                  wait_for_ready=True)
        return query_assets_response.assets

    def delete(self):
        for backend in self.backends:
            backend.delete_project(self._context, self.id)
        Project.delete_project_metadata(self.id)

    @classmethod
    def list_projects(cls, context: 'Context') -> List['Project']:
        for backend in cls._backends:
            if backend.primary:
                results = backend.list_projects(context)
                return results
        raise Exception(f"Failed to list projects")

    def get_settings(self) -> dict:
        for backend in self.backends:
            if backend.primary:
                result = backend.get_project_settings(self, self._metadata_index[self.id]["project_file_id"])
                return result
        raise Exception(f"Failed to load project file for {self.id}")

    def save_settings(self, data: dict) -> str:
        asset_id = None
        filename = None
        for backend in self.backends:
            temp = backend.put_project_settings(self._context, self, data)
            if backend.primary:
                rsplit_res = temp.rsplit('/', 1)
                asset_id = rsplit_res[1] if len(rsplit_res) > 1 else rsplit_res[0]
            if backend.primary_fs:
                filename = temp
        mimetype = "application/json"
        Project.add_asset_metadata(self.id, asset_id, mimetype, filename, project_key="project_file_id")
        return asset_id

    def get_image_asset(self, asset_id: str, use: generation.AssetUse = generation.ASSET_USE_PROJECT) -> Image.Image:
        for backend in self.backends:
            if backend.primary:
                result = backend.get_image_asset(self, asset_id, use)
                return result
        raise Exception(f"Failed to load image asset {asset_id}")

    def put_image_asset(
            self,
            image: Union[Image.Image, np.ndarray],
            use: generation.AssetUse = generation.ASSET_USE_PROJECT
    ):
        results = []
        asset_id = None
        filename = None
        for backend in self.backends:
            result = backend.put_image_asset(self, image, use, asset_id=asset_id)
            if backend.primary:
                rsplit_res = result.rsplit('/', 1)
                asset_id = rsplit_res[1] if len(rsplit_res) > 1 else rsplit_res[0]
                results.append(asset_id)
            if backend.primary_fs:
                filename = result
        mimetype = "image/png"
        Project.add_asset_metadata(self.id, asset_id, mimetype, filename)
        return results

    def get_video_asset(self, asset_id: str, use: generation.AssetUse = generation.ASSET_USE_INPUT) -> bytes:
        for backend in self.backends:
            if backend.primary:
                result = backend.get_video_asset(self, asset_id, use)
                return result
        raise Exception(f"Failed to load video asset {asset_id}")

    def put_video_asset(self, video_path: str) -> List[str]:
        results = []
        filename = None
        asset_id = None
        for backend in self.backends:
            result = backend.put_video_asset(self, video_path, asset_id=asset_id)
            if backend.primary:
                rsplit_res = result.rsplit('/', 1)
                asset_id = rsplit_res[1] if len(rsplit_res) > 1 else rsplit_res[0]
                results.append(asset_id)
            if backend.primary_fs:
                filename = result
        mimetype = "video/mp4"
        Project.add_asset_metadata(self.id, asset_id, mimetype, filename)
        return results

    def update_project(self, title: str = None, file_id: str = None, file_uri: str = None):
        for backend in self.backends:
            result = backend.update_project(self._context, self, title, file_id, file_uri)
        if title:
            self._project.title = title
        if file_id:
            self._project.file.id = file_id
        if file_uri:
            self._project.file.uri = file_uri


    @staticmethod
    def add_asset_metadata(project_id: str, asset_id: str, mime_type: str, filename: str, project_key: str = None) -> None:
        # metadata_index = self.load_metadata_index() # I assume metadata is updated by each operation
        if project_id not in Project._metadata_index:
            Project._metadata_index[project_id] = {}
        Project._metadata_index[project_id][asset_id] = {
            "mime_type": mime_type
        }
        if filename is not None:
            Project._metadata_index[project_id][asset_id]["filename"] = filename
        if project_key is not None:
            Project._metadata_index[project_id][project_key] = asset_id
        Project.save_metadata_index()

    @staticmethod
    def delete_project_metadata(project_id: str) -> None:
        Project._metadata_index.pop(project_id, None)

    @classmethod
    def save_metadata_index(cls, metadata_index: dict = None) -> None:
        if metadata_index is None:
            metadata_index = cls._metadata_index
        index_file = f"metadata_index.json"
        with open(index_file, "w") as f:
            json.dump(metadata_index, f)

    @classmethod
    def load_metadata_index(cls) -> dict:
        index_file = "metadata_index.json"
        if os.path.exists(index_file):
            with open(index_file, "r") as f:
                metadata_index = json.load(f)
            return metadata_index
        return {}


class Context:
    def __init__(self, host: str = "", api_key: str = None, stub: generation_grpc.GenerationServiceStub = None):
        if not host and stub is None:
            raise Exception("Must provide either GRPC host or stub to Api")
        channel = open_channel(host, api_key) if host else None
        if not stub:
            stub = generation_grpc.GenerationServiceStub(channel)

        self._dashboard_stub = dashboard_grpc.DashboardServiceStub(channel) if channel else None
        self._proj_stub = project_grpc.ProjectServiceStub(channel) if channel else None

        self._asset = Endpoint(stub, 'asset-service')
        self._generate = Endpoint(stub, 'stable-diffusion-v1-5')
        self._inpaint = Endpoint(stub, 'stable-inpainting-512-v2-0')
        self._interpolate = Endpoint(stub, 'interpolation-server-v1')
        self._transform = Endpoint(stub, 'transform-server-v1')

        self._debug_no_chains = False
        self._max_retries = 5             # retry request on RPC error
        self._retry_delay = 1.0           # base delay in seconds between retries, each attempt will double
        self._retry_obfuscation = False   # retry request with different seed on classifier obfuscation
        self._retry_schedule_offset = 0.1 # increase schedule start by this amount on each retry after the first

        self._user_organization_id = None
        self._user_profile_picture = None

        logger.warning(
            "\n"
            "The functionality available through this API Context class is in beta and subject to changes in both functionality and pricing.\n"
            "Please be aware that these changes may affect your implementation and usage of this class.\n"
            "\n"
        )

    def generate(
        self,
        prompts: List[str], 
        weights: List[float], 
        width: int = 512, 
        height: int = 512, 
        steps: int = 50, 
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        cfg_scale: float = 7.0, 
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        init_image: Optional[np.ndarray] = None,
        init_strength: float = 0.0,
        init_noise_scale: float = 1.0,
        init_depth: Optional[np.ndarray] = None,
        mask: Optional[np.ndarray] = None,
        masked_area_init: generation.MaskedAreaInit = generation.MASKED_AREA_INIT_ORIGINAL,
        mask_fixup: bool = True,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: float = 0.0,
        return_request: bool = False,
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        """
        Generate an image from a set of weighted prompts.

        :param prompts: List of text prompts
        :param weights: List of prompt weights
        :param width: Width of the generated image
        :param height: Height of the generated image
        :param steps: Number of steps to run the diffusion process
        :param seed: Random seed for the starting noise
        :param samples: Number of samples to generate
        :param cfg_scale: Classifier free guidance scale
        :param sampler: Sampler to use for the diffusion process
        :param init_image: Initial image to use
        :param init_strength: Strength of the initial image
        :param init_noise_scale: Scale of the initial noise
        :param mask: Mask to use (0 for pixels to change, 255 for pixels to keep)
        :param masked_area_init: How to initialize the masked area
        :param mask_fixup: Whether to restore the unmasked area after diffusion
        :param guidance_preset: Preset to use for CLIP guidance
        :param guidance_cuts: Number of cuts to use with CLIP guidance
        :param guidance_strength: Strength of CLIP guidance
        :return: dict mapping artifact type to data
        """
        if not prompts and init_image is None:
            raise ValueError("prompt and/or init_image must be provided")

        if (mask is not None) and (init_image is None) and not return_request:
            raise ValueError("If mask_image is provided, init_image must also be provided")

        p = [generation.Prompt(text=prompt, parameters=generation.PromptParameters(weight=weight)) for prompt,weight in zip(prompts, weights)]
        if init_image is not None:
            p.append(image_to_prompt(init_image))
        if mask is not None:
            p.append(image_to_prompt(mask, type=generation.ARTIFACT_MASK))
        if init_depth is not None:
            p.append(image_to_prompt(init_depth, type=generation.ARTIFACT_DEPTH))

        start_schedule = 1.0 - init_strength
        image_params = self._build_image_params(width, height, sampler, steps, seed, samples, cfg_scale, 
                                                start_schedule, init_noise_scale, masked_area_init, 
                                                guidance_preset, guidance_cuts, guidance_strength)

        request = generation.Request(engine_id=self._generate.engine_id, prompt=p, image=image_params)
        if return_request:
            return request

        results = self._run_request(self._generate, request)

        # optionally force pixels in unmasked areas not to change
        if init_image is not None and mask is not None and mask_fixup:
            results[generation.ARTIFACT_IMAGE] = [image_mix(image, init_image, mask) for image in results[generation.ARTIFACT_IMAGE]]

        return results

    def get_user_info(self) -> Tuple[float, str]:
        """Get the number of credits the user has remaining and their profile picture."""
        if not self._user_organization_id:
            user = self._dashboard_stub.GetMe(dashboard.EmptyRequest())
            self._user_profile_picture = user.profile_picture
            self._user_organization_id = user.organizations[0].organization.id
        organization = self._dashboard_stub.GetOrganization(dashboard.GetOrganizationRequest(id=self._user_organization_id))
        return organization.payment_info.balance * 100, self._user_profile_picture

    def inpaint(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        prompts: List[str], 
        weights: List[float], 
        steps: int = 50, 
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        cfg_scale: float = 7.0, 
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        init_strength: float = 0.0,
        init_noise_scale: float = 1.0,
        masked_area_init: generation.MaskedAreaInit = generation.MASKED_AREA_INIT_ZERO,
        mask_fixup: bool = False,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: float = 0.0,
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        """
        Apply inpainting to an image.

        :param image: Source image
        :param mask: Mask image with 0 for pixels to change and 255 for pixels to keep
        :param prompts: List of text prompts
        :param weights: List of prompt weights
        :param steps: Number of steps to run
        :param seed: Random seed
        :param samples: Number of samples to generate
        :param cfg_scale: Classifier free guidance scale
        :param sampler: Sampler to use for the diffusion process
        :param init_strength: Strength of the initial image
        :param init_noise_scale: Scale of the initial noise
        :param masked_area_init: How to initialize the masked area
        :param mask_fixup: Whether to restore the unmasked area after diffusion
        :param guidance_preset: Preset to use for CLIP guidance
        :param guidance_cuts: Number of cuts to use with CLIP guidance
        :param guidance_strength: Strength of CLIP guidance
        :return: dict mapping artifact type to data
        """
        width, height = image.shape[1], image.shape[0]

        p = [generation.Prompt(text=prompt, parameters=generation.PromptParameters(weight=weight)) for prompt,weight in zip(prompts, weights)]
        if image is not None:
            p.append(image_to_prompt(image))
            if mask is not None:
                p.append(image_to_prompt(mask, type=generation.ARTIFACT_MASK))

        start_schedule = 1.0-init_strength
        image_params = self._build_image_params(width, height, sampler, steps, seed, samples, cfg_scale, 
                                                start_schedule, init_noise_scale, masked_area_init, 
                                                guidance_preset, guidance_cuts, guidance_strength)

        request = generation.Request(engine_id=self._inpaint.engine_id, prompt=p, image=image_params)        
        results = self._run_request(self._inpaint, request)

        # optionally force pixels in unmasked areas not to change
        if mask_fixup:
            results[generation.ARTIFACT_IMAGE] = [image_mix(res_image, image, mask) for res_image in results[generation.ARTIFACT_IMAGE]]

        return results

    def interpolate(
        self,
        images: Iterable[np.ndarray], 
        ratios: List[float],
        mode: generation.InterpolateMode = generation.INTERPOLATE_LINEAR,
    ) -> List[np.ndarray]:
        """
        Interpolate between two images

        :param images: Two images with matching resolution
        :param ratios: In-between ratios to interpolate at
        :param mode: Interpolation mode
        :return: One image for each ratio
        """
        assert len(images) == 2
        assert len(ratios) >= 1

        if len(ratios) == 1:
            if ratios[0] == 0.0:
                return [images[0]]
            elif ratios[0] == 1.0:
                return [images[1]]
            elif mode == generation.INTERPOLATE_LINEAR:
                return [image_mix(images[0], images[1], ratios[0])]

        p = [image_to_prompt(image) for image in images]
        request = generation.Request(
            engine_id=self._interpolate.engine_id,
            prompt=p,
            interpolate=generation.InterpolateParameters(ratios=ratios, mode=mode)
        )

        results = self._run_request(self._interpolate, request)
        return results[generation.ARTIFACT_IMAGE]

    def transform_and_generate(
        self,
        image: np.ndarray,
        params: List[generation.TransformParameters],
        generate_request: generation.Request,
        extras: Optional[Dict] = None,
    ) -> np.ndarray:
        extras_struct = None
        if extras is not None:
            extras_struct = Struct()
            extras_struct.update(extras)

        if not params:
            results = self._run_request(self._generate, generate_request)
            return results[generation.ARTIFACT_IMAGE][0]

        requests = [
            generation.Request(
                engine_id=self._transform.engine_id,
                requested_type=generation.ARTIFACT_TENSOR,
                prompt=[image_to_prompt(image)],
                transform=param,
                extras=extras_struct,
            ) for param in params
        ]

        if self._debug_no_chains:
            prev_result = None
            for rq in requests:
                if prev_result is not None:
                    rq.prompt.pop()
                    rq.prompt.append(tensor_to_prompt(prev_result))
                prev_result = self._run_request(self._transform, rq)[generation.ARTIFACT_TENSOR][0]
            generate_request.prompt.append(tensor_to_prompt(prev_result))
            results = self._run_request(self._generate, generate_request)
        else:
            stages = []
            for idx, rq in enumerate(requests):
                stages.append(generation.Stage(
                    id=str(idx),
                    request=rq, 
                    on_status=[generation.OnStatus(
                        action=[generation.STAGE_ACTION_PASS], 
                        target=str(idx+1)
                    )]
                ))
            stages.append(generation.Stage(
                id=str(len(params)),
                request=generate_request,
                on_status=[generation.OnStatus(
                    action=[generation.STAGE_ACTION_RETURN],
                    target=None
                )]
            ))
            chain_rq = generation.ChainRequest(request_id="xform_gen_chain", stage=stages)
            results = self._run_request(self._transform, chain_rq)

        return results[generation.ARTIFACT_IMAGE][0]

    def transform(
        self,
        images: Iterable[np.ndarray],
        params: Union[generation.TransformParameters, List[generation.TransformParameters]],
        extras: Optional[Dict] = None
    ) -> Tuple[List[np.ndarray], Optional[List[np.ndarray]]]:
        """
        Transform images

        :param images: One or more images to transform
        :param params: Transform operations to apply to each image
        :return: One image artifact for each image and one transform dependent mask
        """
        assert len(images)
        assert isinstance(images[0], np.ndarray)

        extras_struct = None
        if extras is not None:
            extras_struct = Struct()
            extras_struct.update(extras)

        if isinstance(params, List) and len(params) > 1:
            if self._debug_no_chains:
                for param in params:
                    images, mask = self.transform(images, param, extras)
                return images, mask

            assert extras is None
            stages = []
            for idx, param in enumerate(params):
                final = idx == len(params) - 1
                rq = generation.Request(
                    engine_id=self._transform.engine_id,
                    prompt=[image_to_prompt(image) for image in images] if idx == 0 else None,
                    transform=param,
                    extras_struct=extras_struct
                )
                stages.append(generation.Stage(
                    id=str(idx),
                    request=rq, 
                    on_status=[generation.OnStatus(
                        action=[generation.STAGE_ACTION_PASS if not final else generation.STAGE_ACTION_RETURN], 
                        target=str(idx+1) if not final else None
                    )]
                ))
            chain_rq = generation.ChainRequest(request_id="xform_chain", stage=stages)
            results = self._run_request(self._transform, chain_rq)
        else:
            request = generation.Request(
                engine_id=self._transform.engine_id,
                prompt=[image_to_prompt(image) for image in images],
                transform=params[0] if isinstance(params, List) else params,
                extras=extras_struct
            )
            results = self._run_request(self._transform, request)

        images = results.get(generation.ARTIFACT_IMAGE, []) + results.get(generation.ARTIFACT_DEPTH, [])
        masks = results.get(generation.ARTIFACT_MASK, None)
        return images, masks

    # TODO: Add option to do transform using given depth map (e.g. for Blender use cases)
    def transform_3d(
        self, 
        images: Iterable[np.ndarray], 
        depth_calc: generation.TransformParameters,
        transform: generation.TransformParameters,
        extras: Optional[Dict] = None
    ) -> Tuple[List[np.ndarray], Optional[List[np.ndarray]]]:
        assert len(images)
        assert isinstance(images[0], np.ndarray)

        image_prompts = [image_to_prompt(image) for image in images]
        warped_images = []
        warp_mask = None
        op_id = "resample" if transform.HasField("resample") else "camera_pose"

        extras_struct = Struct()
        if extras is not None:
            extras_struct.update(extras)

        rq_depth = generation.Request(
            engine_id=self._transform.engine_id,
            requested_type=generation.ARTIFACT_TENSOR,
            prompt=[image_prompts[0]],
            transform=depth_calc,
        )
        rq_transform = generation.Request(
            engine_id=self._transform.engine_id,
            prompt=image_prompts,
            transform=transform,
            extras=extras_struct
        )

        if self._debug_no_chains:
            results = self._process_response(self._transform.stub.Generate(rq_depth, wait_for_ready=True))
            rq_transform.prompt.append(
                generation.Prompt(
                    artifact=generation.Artifact(
                        type=generation.ARTIFACT_TENSOR,
                        tensor=results[generation.ARTIFACT_TENSOR][0]
                    )
                )
            )
            results = self._run_request(self._transform, rq_transform)
        else:
            chain_rq = generation.ChainRequest(
                request_id=f"{op_id}_3d_chain",
                stage=[
                    generation.Stage(
                        id="depth_calc",
                        request=rq_depth,
                        on_status=[generation.OnStatus(action=[generation.STAGE_ACTION_PASS], target=op_id)]
                    ),
                    generation.Stage(
                        id=op_id,
                        request=rq_transform,
                        on_status=[generation.OnStatus(action=[generation.STAGE_ACTION_RETURN])]
                    )
                ])
            results = self._run_request(self._transform, chain_rq)

        warped_images = results[generation.ARTIFACT_IMAGE]
        warp_mask = results.get(generation.ARTIFACT_MASK, None)

        return warped_images, warp_mask

    def _adjust_request_for_retry(self, request: generation.Request, attempt: int):
        logger.warning(f"  adjusting request, will retry {self._max_retries-attempt} more times")
        request.image.seed[:] = [seed + 1 for seed in request.image.seed]
        if attempt > 0 and request.image.parameters and request.image.parameters[0].HasField("schedule"):
            schedule = request.image.parameters[0].schedule
            if schedule.HasField("start"):
                schedule.start = max(0.0, min(1.0, schedule.start + self._retry_schedule_offset))

    def _build_image_params(self, width, height, sampler, steps, seed, samples, cfg_scale, 
                            schedule_start, init_noise_scale, masked_area_init, 
                            guidance_preset, guidance_cuts, guidance_strength):

        if not seed:
            seed = [random.randrange(0, 4294967295)]
        elif isinstance(seed, int):
            seed = [seed]
        else:
            seed = list(seed)

        step_parameters = {
            "scaled_step": 0,
            "sampler": generation.SamplerParameters(cfg_scale=cfg_scale, init_noise_scale=init_noise_scale),
        }
        if schedule_start != 1.0:
            step_parameters["schedule"] = generation.ScheduleParameters(start=schedule_start)

        if guidance_preset is not generation.GUIDANCE_PRESET_NONE:
            cutouts = generation.CutoutParameters(count=guidance_cuts) if guidance_cuts else None
            if guidance_strength == 0.0:
                guidance_strength = None
            step_parameters["guidance"] = generation.GuidanceParameters(
                guidance_preset=guidance_preset,
                instances=[
                    generation.GuidanceInstanceParameters(
                        cutouts=cutouts,
                        guidance_strength=guidance_strength,
                        models=None, prompt=None
                    )
                ]
            )

        return generation.ImageParameters(
            transform=generation.TransformType(diffusion=sampler),
            height=height,
            width=width,
            seed=seed,
            steps=steps,
            samples=samples,
            masked_area_init=masked_area_init,
            parameters=[generation.StepParameter(**step_parameters)],
        )

    def _process_response(self, response) -> Dict[int, List[np.ndarray]]:
        results: Dict[int, List[np.ndarray]] = {}
        for resp in response:
            for artifact in resp.artifacts:
                if artifact.type not in results:
                    results[artifact.type] = []
                if artifact.type == generation.ARTIFACT_CLASSIFICATIONS:
                    results[artifact.type].append(artifact.classifier)
                elif artifact.type in (generation.ARTIFACT_DEPTH, generation.ARTIFACT_IMAGE, generation.ARTIFACT_MASK):
                    nparr = np.frombuffer(artifact.binary, np.uint8)
                    im = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    results[artifact.type].append(im)
                elif artifact.type == generation.ARTIFACT_TENSOR:
                    results[artifact.type].append(artifact.tensor)
                elif artifact.type == generation.ARTIFACT_TEXT:
                    results[artifact.type].append(artifact.text)
                elif artifact.type == generation.ARTIFACT_VIDEO:
                    results[artifact.type].append(artifact.binary)
        return results

    def _run_request(
        self, 
        endpoint: Endpoint, 
        request: Union[generation.ChainRequest, generation.Request]
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        for attempt in range(self._max_retries+1):
            try:
                if isinstance(request, generation.Request):
                    assert endpoint.engine_id == request.engine_id
                    response = endpoint.stub.Generate(request, wait_for_ready=True)
                else:
                    response = endpoint.stub.ChainGenerate(request, wait_for_ready=True)

                results = self._process_response(response)

                # check for classifier obfuscation
                if generation.ARTIFACT_CLASSIFICATIONS in results:
                    for classifier in results[generation.ARTIFACT_CLASSIFICATIONS]:
                        if classifier.realized_action == generation.ACTION_OBFUSCATE:
                            raise ClassifierException(classifier)

                break
            except ClassifierException as ce:
                if attempt == self._max_retries or not self._retry_obfuscation:
                    raise ce

                for exceed in ce.classifier_result.exceeds:
                    logger.warning(f"Received classifier obfuscation. Exceeded {exceed.name} threshold")
                    for concept in exceed.concepts:
                        if concept.HasField("threshold"):
                            logger.warning(f"  {concept.concept} ({concept.threshold})")

                if isinstance(request, generation.Request) and request.HasField("image"):
                    self._adjust_request_for_retry(request, attempt)
                elif isinstance(request, generation.ChainRequest):
                    for stage in request.stage:
                        if stage.request.HasField("image"):
                            self._adjust_request_for_retry(stage.request, attempt)
                else:
                    raise ce
            except grpc.RpcError as rpc_error:
                if hasattr(rpc_error, "code") and rpc_error.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    raise OutOfCreditsException(rpc_error.details())

                if attempt == self._max_retries:
                    raise rpc_error

                logger.warning(f"Received RpcError: {rpc_error} will retry {self._max_retries-attempt} more times")
                time.sleep(self._retry_delay * 2**attempt)
        return results
