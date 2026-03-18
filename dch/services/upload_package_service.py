"""
DCHUploadPackageService
===============================================================================

Helper service for interpreting an uploaded session as a known file package.

This service sits on top of `DCHUploadSessionService`.

Why this exists
---------------
`DCHUploadSessionService` only knows how to:
- create upload sessions
- save uploaded files
- read session manifest
- list files
- locate files on disk
- cleanup sessions

It does NOT know what the uploaded files *mean*.

`DCHUploadPackageService` adds package-level understanding for specific formats.

Currently supported package types
---------------------------------
1. Shapefile package
2. GeoTIFF package

Responsibilities
----------------
- validate required file presence for a package
- validate shapefile basename consistency
- resolve the main file path for downstream processing
- return normalized package metadata for router/service use

Non-responsibilities
--------------------
- does not save files
- does not read GeoDataFrames
- does not read rasters
- does not import to database
- does not append features
- does not perform CRS analysis

Those belong in specialized processing services.

===============================================================================
USAGE EXAMPLE: SHAPEFILE
===============================================================================

from app.dch.services.upload_session_service import DCHUploadSessionService
from app.dch.services.upload_package_service import DCHUploadPackageService
from settings import MEDIA_DIR
from utils.singletons import da_logger

upload_service = DCHUploadSessionService(
    media_dir=MEDIA_DIR,
    logger=da_logger,
)

package_service = DCHUploadPackageService(
    upload_service=upload_service
)

form = await request.form()

upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
    form=form,
    upload_type="shapefile",
    metadata={"purpose": "vector_append_preview"},
)

shape_info = package_service.resolve_shapefile(upload_id)

# Main .shp file for geopandas
shp_fp = shape_info["main_file"]

# Example returned structure:
# {
#   "upload_id": "...",
#   "package_type": "shapefile",
#   "main_file": "/abs/path/roads.shp",
#   "base_name": "roads",
#   "files": {
#       ".shp": "roads.shp",
#       ".shx": "roads.shx",
#       ".dbf": "roads.dbf",
#       ".prj": "roads.prj"
#   }
# }

===============================================================================
USAGE EXAMPLE: GEOTIFF
===============================================================================

from app.dch.services.upload_session_service import DCHUploadSessionService
from app.dch.services.upload_package_service import DCHUploadPackageService

upload_service = DCHUploadSessionService(
    media_dir=MEDIA_DIR,
    logger=da_logger,
)

package_service = DCHUploadPackageService(
    upload_service=upload_service
)

form = await request.form()

upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
    form=form,
    upload_type="geotiff",
    metadata={"purpose": "raster_import"},
)

tif_info = package_service.resolve_geotiff(upload_id)

# Main raster file
tif_fp = tif_info["main_file"]

# Example returned structure:
# {
#   "upload_id": "...",
#   "package_type": "geotiff",
#   "main_file": "/abs/path/elevation.tif",
#   "base_name": "elevation",
#   "files": {
#       ".tif": "elevation.tif"
#   }
# }

===============================================================================
COMMON ROUTER USAGE
===============================================================================

# Save files first using upload session service
upload_id, session_dir, _ = await upload_service.save_files_from_form(
    form=form,
    upload_type="shapefile",
    metadata={"purpose": "vector_import"},
)

# Then resolve package semantics using package service
shape_info = package_service.resolve_shapefile(upload_id)
shp_fp = shape_info["main_file"]

# Pass shp_fp to GeoPandas or another processor

===============================================================================
ERROR CASES
===============================================================================

This service raises HTTPException when:
- upload session has no files
- required shapefile parts are missing
- shapefile parts do not share the same basename
- no .tif / .tiff exists for GeoTIFF package

===============================================================================
FUTURE EXTENSION IDEAS
===============================================================================

Later you can add methods like:
- resolve_geopackage(upload_id)
- resolve_csv(upload_id)
- resolve_zip(upload_id)
- resolve_geojson(upload_id)

This keeps upload storage generic while package resolution remains format-aware.
"""

import os
from typing import Dict, List, Optional

from fastapi import HTTPException

from app.damapql.dch.services.upload_session_service import DCHUploadSessionService


class DCHUploadPackageService:
    """
    Resolves uploaded file sessions into known package types.

    Supported:
    - Shapefile
    - GeoTIFF
    """

    def __init__(self, upload_service: DCHUploadSessionService):
        self.upload_service = upload_service

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------
    def _list_files(self, upload_id: str) -> List[str]:
        """
        Return uploaded file names for the given session.
        """
        return self.upload_service.list_uploaded_files(upload_id)

    def _find_by_extensions(self, upload_id: str, extensions: List[str]) -> Optional[str]:
        """
        Find the first file path in the session matching any of the provided
        extensions, in order.

        Example:
            _find_by_extensions(upload_id, [".tif", ".tiff"])
        """
        for ext in extensions:
            fp = self.upload_service.find_first_file_by_extension(upload_id, ext)
            if fp:
                return fp
        return None

    def _build_extension_map(self, file_names: List[str]) -> Dict[str, str]:
        """
        Build a mapping of extension -> filename using the first matching file
        for each extension.

        Example:
            ["roads.shp", "roads.shx", "roads.dbf"]
            ->
            {
                ".shp": "roads.shp",
                ".shx": "roads.shx",
                ".dbf": "roads.dbf"
            }
        """
        ext_map: Dict[str, str] = {}

        for name in file_names:
            _, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext and ext not in ext_map:
                ext_map[ext] = name

        return ext_map

    # -------------------------------------------------------------------------
    # Shapefile package
    # -------------------------------------------------------------------------
    def resolve_shapefile(self, upload_id: str) -> Dict[str, object]:
        """
        Validate and resolve a shapefile package from an upload session.

        Required shapefile parts:
        - .shp
        - .shx
        - .dbf

        Optional:
        - .prj

        Validation rules:
        - required parts must exist
        - required parts must share the same basename
          Example:
            roads.shp
            roads.shx
            roads.dbf

        Returns:
            {
              "upload_id": str,
              "package_type": "shapefile",
              "main_file": str,    # absolute path to .shp
              "base_name": str,    # basename without extension
              "files": {
                  ".shp": "roads.shp",
                  ".shx": "roads.shx",
                  ".dbf": "roads.dbf",
                  ".prj": "roads.prj"   # if present and matching basename
              }
            }
        """
        file_names = self._list_files(upload_id)
        if not file_names:
            raise HTTPException(status_code=400, detail="No uploaded files found in session")

        required = [".shp", ".shx", ".dbf"]

        found: Dict[str, str] = {}
        missing: List[str] = []

        # Find required shapefile parts
        for ext in required:
            match = next((name for name in file_names if name.lower().endswith(ext)), None)
            if match:
                found[ext] = match
            else:
                missing.append(ext)

        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required shapefile parts: {', '.join(missing)}",
            )

        # Ensure all required shapefile parts share same basename
        first_name = next(iter(found.values()))
        base_name = os.path.splitext(first_name)[0].lower()

        for _, name in found.items():
            if os.path.splitext(name)[0].lower() != base_name:
                raise HTTPException(
                    status_code=400,
                    detail="Shapefile parts do not share the same base filename",
                )

        # Optional .prj file if it matches same basename
        prj_name = next((name for name in file_names if name.lower().endswith(".prj")), None)
        if prj_name and os.path.splitext(prj_name)[0].lower() == base_name:
            found[".prj"] = prj_name

        shp_fp = self.upload_service.find_first_file_by_extension(upload_id, ".shp")
        if not shp_fp:
            raise HTTPException(status_code=400, detail="Missing .shp file")

        return {
            "upload_id": upload_id,
            "package_type": "shapefile",
            "main_file": shp_fp,
            "base_name": os.path.splitext(os.path.basename(shp_fp))[0],
            "files": found,
        }

    # -------------------------------------------------------------------------
    # GeoTIFF package
    # -------------------------------------------------------------------------
    def resolve_geotiff(self, upload_id: str) -> Dict[str, object]:
        """
        Validate and resolve a GeoTIFF package from an upload session.

        Required:
        - .tif OR .tiff

        Optional future sidecars (not enforced in current version):
        - .tfw
        - .prj
        - .ovr
        - .aux.xml

        Validation rules:
        - at least one .tif or .tiff must exist

        Returns:
            {
              "upload_id": str,
              "package_type": "geotiff",
              "main_file": str,   # absolute path to .tif/.tiff
              "base_name": str,
              "files": {
                  ".tif": "elevation.tif"
              }
            }

        Notes:
        - Same-basename validation is not required in this first version.
        - Sidecars can be added later if needed.
        """
        file_names = self._list_files(upload_id)
        if not file_names:
            raise HTTPException(status_code=400, detail="No uploaded files found in session")

        tif_fp = self._find_by_extensions(upload_id, [".tif", ".tiff"])
        if not tif_fp:
            raise HTTPException(status_code=400, detail="Missing .tif or .tiff file")

        ext_map = self._build_extension_map(file_names)

        main_name = os.path.basename(tif_fp)
        _, main_ext = os.path.splitext(main_name)
        main_ext = main_ext.lower()

        found = {
            main_ext: main_name
        }

        return {
            "upload_id": upload_id,
            "package_type": "geotiff",
            "main_file": tif_fp,
            "base_name": os.path.splitext(os.path.basename(tif_fp))[0],
            "files": found,
        }