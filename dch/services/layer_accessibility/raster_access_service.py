# app/damapql/dch/services/layer_accessibility/raster_access_service.py
from __future__ import annotations

from pathlib import Path

from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from fastapi import HTTPException

from app.damapql.dch.models import LayerInfo
from app.damapql.settings import MEDIA_BASE


class RasterAccessService:
    """
    Open a raster based on LayerAccessibility configuration.
            LayerInfo
               ↓
            LayerAccessibility
               ↓
            accessibility_params
               │
               ├── MinIO / S3
               │     ↓
               │   COGRaster.open_from_minio()
               │
               └── File
                     ↓
                 MEDIA_BASE / relative path or absolute path
                     ↓
                 COGRaster.open_from_local()
    """

    @staticmethod
    def resolve_local_path(params: object) -> Path:
        raw_fp = getattr(params, "file_path", None)
        if not raw_fp:
            raise HTTPException(status_code=404, detail="Raster file path missing")

        is_media_rel = (
            getattr(params, "is_path_media_rel", False)
            or getattr(params, "path_base", None) == "media_rel"
        )
        if is_media_rel:
            media_root = Path(MEDIA_BASE).resolve()
            rel_parts = [p for p in str(raw_fp).replace("\\", "/").split("/") if p and p != "."]
            file_path = (media_root.joinpath(*rel_parts)).resolve()
            if not file_path.is_relative_to(media_root):
                raise HTTPException(status_code=400, detail="Invalid media-relative raster path")
        else:
            file_path = Path(raw_fp)

        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Raster file not found: {file_path}")
        return file_path

    @classmethod
    def open_cog(cls, info: LayerInfo, minio_manager: MinioManager | None = None) -> COGRaster:
        access = info.layer_accessibility
        if not access:
            raise HTTPException(status_code=404, detail="Layer accessibility configuration missing")

        params = access.accessibility_params
        if not params:
            raise HTTPException(status_code=404, detail="Layer accessibility parameters missing")

        if access.is_minio or access.is_s3:
            if minio_manager is None:
                raise HTTPException(status_code=503, detail="MinIO service is unavailable")

            raster = COGRaster.open_from_minio(params, minio_manager)
            if raster is None:
                raise HTTPException(status_code=404, detail="Unable to open MinIO COG raster")
            return raster

        file_path = cls.resolve_local_path(params)
        raster = COGRaster.open_from_local(str(file_path))
        if raster is None:
            raise HTTPException(status_code=500, detail=f"Unable to open local COG raster: {file_path}")
        return raster