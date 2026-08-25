"""
DCHUploadSessionService
===============================================================================

Generic upload session manager for temporary file-based uploads.

Why this exists
---------------
This service provides a reusable upload/session layer for different file types,
instead of making every workflow implement its own temp-file logic.

Current supported usage
-----------------------
- Shapefile uploads (.shp + .shx + .dbf [+ .prj optional])
- GeoTIFF uploads (.tif / .tiff)

Future-ready for
----------------
- GeoPackage
- CSV
- ZIP packages
- resumable/chunked uploads
- raster sidecar handling (.ovr, .aux.xml, .tfw)
- async workers pipelines

What this service does
----------------------
- Creates or reuses an upload session
- Saves multipart uploaded files into a temp session folder
- Writes/updates a manifest file (session.json)
- Lists uploaded files
- Finds files by extension
- Cleans up the entire upload session

What this service does NOT do
-----------------------------
- It does not interpret spatial data
- It does not validate file semantics deeply
- It does not read shapefiles or geotiffs
- It does not append/import into DB

Those responsibilities belong in separate validator/processor services.

------------------------------------------------------------------------------
USAGE EXAMPLE: SHAPEFILE
------------------------------------------------------------------------------
from app.dch.services.upload_session_service import DCHUploadSessionService
from settings import MEDIA_DIR
from utils.singletons import da_logger

upload_service = DCHUploadSessionService(
    media_dir=MEDIA_DIR,
    logger=da_logger,
)

form = await request.form()

upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
    form=form,
    upload_type="shapefile",
    metadata={"purpose": "vector_append_preview"},
)

# Find .shp file later
shp_fp = upload_service.find_first_file_by_extension(upload_id, ".shp")

------------------------------------------------------------------------------
USAGE EXAMPLE: GEOTIFF
------------------------------------------------------------------------------
upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
    form=form,
    upload_type="geotiff",
    metadata={"purpose": "raster_import"},
)

tif_fp = upload_service.find_first_file_by_extension(upload_id, ".tif")
if not tif_fp:
    tif_fp = upload_service.find_first_file_by_extension(upload_id, ".tiff")

------------------------------------------------------------------------------
MANIFEST EXAMPLE (session.json)
------------------------------------------------------------------------------
{
  "upload_id": "uuid",
  "upload_type": "shapefile",
  "status": "files_uploaded",
  "files": [
    {
      "name": "roads.shp",
      "size": 15360,
      "saved_at": "2026-03-09T20:12:00.000000"
    }
  ],
  "metadata": {
    "purpose": "vector_append_preview"
  },
  "created_at": "...",
  "updated_at": "..."
}
"""

import json
import os
import shutil
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException


class DCHUploadSessionService:
    """
    Generic upload session service for temporary file storage.
    """

    MANIFEST_FILENAME = "session.json"

    def __init__(self, media_dir: str, temp_subdir: str = "upload_sessions", logger=None):
        self.media_dir = media_dir
        self.temp_upload_dir = os.path.join(media_dir, temp_subdir)
        self.logger = logger

    # -------------------------------------------------------------------------
    # Logging helper
    # -------------------------------------------------------------------------
    def log_exception(self, msg: str) -> None:
        if self.logger:
            self.logger.exception(msg)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    def get_session_dir(self, upload_id: str) -> str:
        """
        Return absolute directory path for a given upload session.
        """
        return os.path.join(self.temp_upload_dir, upload_id)

    def get_manifest_path(self, upload_id: str) -> str:
        """
        Return absolute path to the session manifest file.
        """
        return os.path.join(self.get_session_dir(upload_id), self.MANIFEST_FILENAME)

    # -------------------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------------------
    def create_session(
        self,
        upload_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new upload session and write its manifest.

        Args:
            upload_type:
                Logical upload type such as:
                - "shapefile"
                - "geotiff"
                - "vector_append"
                - "raster_import"

            metadata:
                Optional extra metadata to store in session.json

        Returns:
            Manifest dictionary.
        """
        os.makedirs(self.temp_upload_dir, exist_ok=True)

        upload_id = str(uuid.uuid4())
        session_dir = self.get_session_dir(upload_id)
        os.makedirs(session_dir, exist_ok=True)

        manifest = {
            "upload_id": upload_id,
            "upload_type": upload_type,
            "status": "created",
            "files": [],
            "metadata": metadata or {},
            "created_at": self._now(),
            "updated_at": self._now(),
        }

        self.write_manifest(upload_id, manifest)
        return manifest

    def read_manifest(self, upload_id: str) -> Dict[str, Any]:
        """
        Read session.json for an existing upload session.
        """
        manifest_path = self.get_manifest_path(upload_id)

        if not os.path.exists(manifest_path):
            raise HTTPException(status_code=404, detail="Upload session not found")

        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_manifest(self, upload_id: str, manifest: Dict[str, Any]) -> None:
        """
        Persist the manifest to disk.
        """
        manifest["updated_at"] = self._now()

        with open(self.get_manifest_path(upload_id), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    def update_manifest(self, upload_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update existing manifest with a shallow patch.
        """
        manifest = self.read_manifest(upload_id)
        manifest.update(patch)
        self.write_manifest(upload_id, manifest)
        return manifest

    # -------------------------------------------------------------------------
    # File saving
    # -------------------------------------------------------------------------
    async def save_files_from_request(
        self,
        request,
        upload_type: str,
        upload_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, List[str]]:
        """
        Convenience wrapper when caller wants to pass FastAPI Request directly.

        Returns:
            (upload_id, session_dir, saved_file_paths)
        """
        form = await request.form()
        return await self.save_files_from_form(
            form=form,
            upload_type=upload_type,
            upload_id=upload_id,
            metadata=metadata,
        )

    async def save_files_from_form(
        self,
        form,
        upload_type: str,
        upload_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, List[str]]:
        """
        Save uploaded multipart files into a session folder.

        If upload_id is not supplied, a new session is created.
        If upload_id is supplied, files are appended/updated in that session.

        Returns:
            upload_id,
            session_dir,
            saved_file_paths
        """
        upload_files = [value for _, value in form.items() if hasattr(value, "filename")]

        if not upload_files:
            raise HTTPException(status_code=400, detail="No files provided")

        if upload_id:
            manifest = self.read_manifest(upload_id)
            session_dir = self.get_session_dir(upload_id)
        else:
            manifest = self.create_session(upload_type=upload_type, metadata=metadata)
            upload_id = manifest["upload_id"]
            session_dir = self.get_session_dir(upload_id)

        os.makedirs(session_dir, exist_ok=True)

        saved_files: List[str] = []
        existing_files = {f["name"]: f for f in manifest.get("files", [])}

        for file in upload_files:
            if not file.filename:
                continue

            file_path = os.path.join(session_dir, file.filename)

            with open(file_path, "wb") as f:
                shutil.copyfileobj(file.file, f)

            size = os.path.getsize(file_path)
            saved_files.append(file_path)

            existing_files[file.filename] = {
                "name": file.filename,
                "size": size,
                "saved_at": self._now(),
            }

        manifest["upload_type"] = upload_type
        manifest["status"] = "files_uploaded"
        manifest["metadata"] = {**manifest.get("metadata", {}), **(metadata or {})}
        manifest["files"] = list(existing_files.values())

        self.write_manifest(upload_id, manifest)

        return upload_id, session_dir, saved_files

    # -------------------------------------------------------------------------
    # File access helpers
    # -------------------------------------------------------------------------
    def list_uploaded_files(self, upload_id: str) -> List[str]:
        """
        Return list of uploaded file names for a session.
        """
        manifest = self.read_manifest(upload_id)
        return [f["name"] for f in manifest.get("files", [])]

    def get_file_paths(self, upload_id: str) -> List[str]:
        """
        Return absolute existing file paths for a session.
        """
        session_dir = self.get_session_dir(upload_id)
        manifest = self.read_manifest(upload_id)

        result = []
        for f in manifest.get("files", []):
            fp = os.path.join(session_dir, f["name"])
            if os.path.exists(fp):
                result.append(fp)

        return result

    def find_first_file_by_extension(self, upload_id: str, extension: str) -> Optional[str]:
        """
        Find the first uploaded file matching a given extension.

        Example:
            find_first_file_by_extension(upload_id, ".shp")
            find_first_file_by_extension(upload_id, ".tif")
        """
        extension = extension.lower()

        for fp in self.get_file_paths(upload_id):
            if fp.lower().endswith(extension):
                return fp

        return None

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    def cleanup_session(self, upload_id: str) -> None:
        """
        Remove the entire upload session folder from disk.
        """
        session_dir = self.get_session_dir(upload_id)
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)