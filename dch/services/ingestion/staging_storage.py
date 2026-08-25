from __future__ import annotations

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator, Optional

from fastapi import UploadFile

from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

BUFFER_SIZE = 1024 * 1024  # 1 MB chunk buffer


@dataclass
class StagedFile:
    """
    Represents an object currently available in staging.

    local_path:
        Local filesystem path for GDAL/Rasterio workers.
    staging_dir:
        Explicit directory containing the staged local file.
    bucket_name/object_name:
        Populated when staging is backed by object storage.
    """
    local_path: str
    staging_dir: Optional[str] = None
    bucket_name: Optional[str] = None
    object_name: Optional[str] = None

    @property
    def is_remote(self) -> bool:
        return bool(self.bucket_name and self.object_name)

    @property
    def s3_uri(self) -> Optional[str]:
        if not self.is_remote:
            return None
        return f"s3://{self.bucket_name}/{self.object_name.lstrip('/')}"


class StagingStorage(ABC):
    """
    Abstract landing storage interface for incoming data and worker handoff.
    """

    @abstractmethod
    def stage_upload(self, upload_file: UploadFile, filename: str) -> StagedFile:
        """Saves incoming upload to staging workspace."""
        pass

    @abstractmethod
    def cleanup(self, staged_file: StagedFile) -> None:
        """Removes local and/or remote staging artifacts."""
        pass

    @contextmanager
    def stage(self, upload_file: UploadFile, filename: str) -> Generator[StagedFile, None, None]:
        """Context manager providing automatic cleanup of staging assets."""
        staged = self.stage_upload(upload_file, filename)
        try:
            yield staged
        finally:
            self.cleanup(staged)


class LocalStagingStorage(StagingStorage):
    """
    Local filesystem staging workspace for synchronous workers & GDAL processing.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir
        if self.base_dir:
            os.makedirs(self.base_dir, exist_ok=True)

    def stage_upload(self, upload_file: UploadFile, filename: str) -> StagedFile:
        staging_dir = tempfile.mkdtemp(prefix="digitalarz_ingest_", dir=self.base_dir)
        local_path = os.path.join(staging_dir, filename)

        try:
            upload_file.file.seek(0)
            with open(local_path, "wb") as dst:
                while chunk := upload_file.file.read(BUFFER_SIZE):
                    dst.write(chunk)

            da_logger.info(f"File staged locally: {local_path}")
            return StagedFile(local_path=local_path, staging_dir=staging_dir)

        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def cleanup(self, staged_file: StagedFile) -> None:
        target_dir = staged_file.staging_dir or (
            os.path.dirname(staged_file.local_path) if staged_file.local_path else None)
        if target_dir and os.path.isdir(target_dir):
            try:
                shutil.rmtree(target_dir, ignore_errors=True)
                da_logger.debug(f"Local staging cleaned: {target_dir}")
            except Exception as e:
                da_logger.warning(f"Failed to clean staging directory {target_dir}: {e}")


class MinioStagingStorage(StagingStorage):
    """
    MinIO-backed durable staging with local scratch copy for GDAL/Rasterio.
    """

    def __init__(self, minio_manager: MinioManager, bucket_name: str, prefix: str = "staging",
            local_base_dir: Optional[str] = None, delete_remote_on_cleanup: bool = True, ):
        self.minio_manager = minio_manager
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self.local_base_dir = local_base_dir
        self.delete_remote_on_cleanup = delete_remote_on_cleanup

        if self.local_base_dir:
            os.makedirs(self.local_base_dir, exist_ok=True)

    def stage_upload(self, upload_file: UploadFile, filename: str) -> StagedFile:
        staging_dir = tempfile.mkdtemp(prefix="digitalarz_ingest_", dir=self.local_base_dir)
        local_path = os.path.join(staging_dir, filename)
        object_name = f"{self.prefix}/{filename}"

        try:
            # 1. Local working scratch
            upload_file.file.seek(0)
            with open(local_path, "wb") as dst:
                while chunk := upload_file.file.read(BUFFER_SIZE):
                    dst.write(chunk)

            # 2. Durable MinIO staging copy
            uploaded = self.minio_manager.upload_file(bucket_name=self.bucket_name, object_name=object_name,
                file_path=local_path, content_type="application/octet-stream", )

            if not uploaded:
                raise RuntimeError(f"Failed to upload file to MinIO staging: s3://{self.bucket_name}/{object_name}")

            da_logger.info(f"File staged in MinIO: s3://{self.bucket_name}/{object_name}")

            return StagedFile(local_path=local_path, staging_dir=staging_dir, bucket_name=self.bucket_name,
                object_name=object_name, )

        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def cleanup(self, staged_file: StagedFile) -> None:
        # 1. Clean local scratch
        target_dir = staged_file.staging_dir or (
            os.path.dirname(staged_file.local_path) if staged_file.local_path else None)
        if target_dir and os.path.isdir(target_dir):
            try:
                shutil.rmtree(target_dir, ignore_errors=True)
                da_logger.debug(f"Local staging scratch cleaned: {target_dir}")
            except Exception as e:
                da_logger.warning(f"Failed to clean local staging: {e}")

        # 2. Clean remote staging object
        if self.delete_remote_on_cleanup and staged_file.bucket_name and staged_file.object_name:
            try:
                self.minio_manager.delete_object(staged_file.bucket_name, staged_file.object_name)
                da_logger.debug(f"Deleted MinIO staging object: {staged_file.s3_uri}")
            except Exception as e:
                da_logger.warning(f"Failed to clean MinIO staging object {staged_file.s3_uri}: {e}")
