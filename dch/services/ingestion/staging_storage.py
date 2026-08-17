from __future__ import annotations

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from fastapi import UploadFile

from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


@dataclass
class StagedFile:
    """
    Represents an object currently available in staging.

    local_path:
        Local processing path. GDAL/Rasterio can use this directly.

    bucket_name/object_name:
        Populated when staging is backed by object storage.
    """

    local_path: str

    bucket_name: Optional[str] = None
    object_name: Optional[str] = None

    @property
    def is_remote(self) -> bool:
        return bool(self.bucket_name and self.object_name)

    @property
    def s3_uri(self) -> Optional[str]:
        if not self.is_remote:
            return None

        return (f"s3://{self.bucket_name}/"
                f"{self.object_name.lstrip('/')}")


class StagingStorage(ABC):
    """
    Temporary/durable landing storage used during ingestion.

    This is intentionally separate from Managed Storage.

    StagingStorage:
        Incoming data / processing handoff.

    Managed Storage:
        Final persistent data location.
    """

    @abstractmethod
    def stage_upload(self, upload_file: UploadFile, filename: str, ) -> StagedFile:
        pass

    @abstractmethod
    def cleanup(self, staged_file: StagedFile, ) -> None:
        pass


class LocalStagingStorage(StagingStorage):
    """
    Local staging using an application-controlled temporary directory.

    Suitable for:
    - synchronous processing
    - local installations
    - GDAL/Rasterio scratch workspace
    """

    def __init__(self, base_dir: Optional[str] = None, ):
        self.base_dir = base_dir

        if self.base_dir:
            os.makedirs(self.base_dir, exist_ok=True, )

    def stage_upload(self, upload_file: UploadFile, filename: str, ) -> StagedFile:

        staging_dir = tempfile.mkdtemp(prefix="digitalarz_ingest_", dir=self.base_dir, )

        local_path = os.path.join(staging_dir, filename, )

        try:
            upload_file.file.seek(0)

            with open(local_path, "wb", ) as dst:
                shutil.copyfileobj(upload_file.file, dst, )

            da_logger.info(f"File staged locally: {local_path}")

            return StagedFile(local_path=local_path, )

        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True, )
            raise

    def cleanup(self, staged_file: StagedFile, ) -> None:

        local_path = staged_file.local_path

        if not local_path:
            return

        staging_dir = os.path.dirname(local_path)

        try:
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True, )

                da_logger.debug(f"Local staging cleaned: "
                                f"{staging_dir}")

        except Exception as e:
            da_logger.warning(f"Failed to clean staging directory "
                              f"{staging_dir}: {e}")


class MinioStagingStorage(StagingStorage):
    """
    MinIO-backed staging.

    IMPORTANT:
    A local working copy is still created because GDAL/Rasterio
    processing may require a filesystem path.

    MinIO provides the durable staging/handoff location.
    The local file is processing scratch space.
    """

    def __init__(self, minio_manager: MinioManager, bucket_name: str, prefix: str = "staging",
            local_base_dir: Optional[str] = None, delete_remote_on_cleanup: bool = True, ):
        self.minio_manager = minio_manager
        self.bucket_name = bucket_name

        self.prefix = prefix.strip("/")

        self.local_base_dir = local_base_dir

        self.delete_remote_on_cleanup = (delete_remote_on_cleanup)

        if self.local_base_dir:
            os.makedirs(self.local_base_dir, exist_ok=True, )

    def stage_upload(self, upload_file: UploadFile, filename: str, ) -> StagedFile:

        staging_dir = tempfile.mkdtemp(prefix="digitalarz_ingest_", dir=self.local_base_dir, )

        local_path = os.path.join(staging_dir, filename, )

        object_name = (f"{self.prefix}/"
                       f"{filename}")

        try:
            # ----------------------------------------
            # Local scratch copy
            # ----------------------------------------

            upload_file.file.seek(0)

            with open(local_path, "wb", ) as dst:
                shutil.copyfileobj(upload_file.file, dst, )

            # ----------------------------------------
            # Durable staging copy
            # ----------------------------------------

            uploaded = self.minio_manager.upload_file(bucket_name=self.bucket_name, object_name=object_name,
                file_path=local_path, content_type="application/octet-stream", )

            if not uploaded:
                raise RuntimeError("Failed to upload file "
                                   "to MinIO staging.")

            da_logger.info("File staged in MinIO: "
                           f"s3://{self.bucket_name}/{object_name}")

            return StagedFile(local_path=local_path, bucket_name=self.bucket_name, object_name=object_name, )

        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True, )
            raise

    def cleanup(self, staged_file: StagedFile, ) -> None:

        # --------------------------------------------
        # Delete local scratch
        # --------------------------------------------

        if staged_file.local_path:

            staging_dir = os.path.dirname(staged_file.local_path)

            try:
                shutil.rmtree(staging_dir, ignore_errors=True, )

            except Exception as e:
                da_logger.warning("Failed to clean local staging: "
                                  f"{e}")

        # --------------------------------------------
        # Delete MinIO staging object
        # --------------------------------------------

        if (self.delete_remote_on_cleanup and staged_file.bucket_name and staged_file.object_name):
            try:
                self.minio_manager.delete_object(staged_file.bucket_name, staged_file.object_name, )

                da_logger.debug("Deleted MinIO staging object: "
                                f"{staged_file.s3_uri}")

            except Exception as e:
                da_logger.warning("Failed to clean MinIO staging object "
                                  f"{staged_file.s3_uri}: {e}")
