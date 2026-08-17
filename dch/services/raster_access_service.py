from fastapi import HTTPException

from app.damapql.dch.models import LayerInfo
from app.damapql.settings import MEDIA_BASE
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager


class RasterAccessService:
    """
    Open a raster based on LayerAccessibility configuration.
            LayerInfo
               ↓
            LayerAccessibility
               ↓
            accessibility_params
               │
               ├── MinIO
               │     ↓
               │ COGRaster.open_from_minio()
               │
               └── File
                     ↓
                 MEDIA_BASE / relative path
                     or
                 absolute path
                     ↓
                 COGRaster.open_from_local()
    MinIO:
        Uses MinioAccessibilityParams and MinioManager.

    Local:
        Uses FileAccessibilityParams.
        Supports media-relative and absolute paths.
    """

    @staticmethod
    def open_cog(info: LayerInfo, minio_manager: MinioManager | None = None, ) -> COGRaster:

        access = info.layer_accessibility

        if not access:
            raise HTTPException(status_code=404, detail="Layer accessibility configuration missing", )

        params = access.accessibility_params

        if not params:
            raise HTTPException(status_code=404, detail="Layer accessibility parameters missing", )

        if access.is_minio:
            if minio_manager is None:
                raise HTTPException(status_code=503, detail="MinIO service is unavailable", )

            return COGRaster.open_from_minio(params, minio_manager, )

        file_path = (MEDIA_BASE / params.file_path if (
                getattr(params, "is_path_media_rel", False) or getattr(params, "path_base",
                                                                       None) == "media_rel") else params.file_path)

        return COGRaster.open_from_local(file_path)
