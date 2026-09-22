from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.damapql.dch.models import LayerInfo, DataModel
from app.damapql.dch.services.raster_access_service import RasterAccessService
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


@dataclass
class RasterTileResult:
    content: bytes
    media_type: str = "image/png"


class RasterTileService:
    DEFAULT_TILE_SIZE = 256

    @staticmethod
    def get_layer(uuid: str, ) -> LayerInfo:

        info = LayerInfo.get_layer_info_by_uuid(uuid)

        if (info is None or not info.data_model or info.data_model.code != DataModel.R.name):
            raise HTTPException(status_code=404, detail="Layer not found or not raster", )

        return info


    @classmethod
    def render_tile(
            cls,
            *,
            uuid: str,
            z: int,
            x: int,
            y: int,
            minio_manager: MinioManager | None = None,
            tile_size: int | None = None,
            custom_color_map: Any = None,  # <-- Pass custom colormap
    ) -> RasterTileResult:

        tile_size = tile_size or cls.DEFAULT_TILE_SIZE

        try:
            info = cls.get_layer(uuid)

            # 1. Open raster (MinIO or Local)
            cog_raster = RasterAccessService.open_cog(info, minio_manager)

            # 2. Styling: custom_color_map takes priority, then layer_styling, else grayscale
            if custom_color_map is not None:
                color_map = custom_color_map
            elif info.layer_styling:
                color_map = COGRaster.create_color_map(info.layer_styling)
            else:
                color_map = None

            # 3. Read XYZ tile
            data = cog_raster.read_tile_as_png(
                x=x,
                y=y,
                z=z,
                color_map=color_map,
                tile_size=tile_size,
            )

            da_logger.debug(f"[RasterTile] SUCCESS uuid={uuid}, z={z}, x={x}, y={y}")

            return RasterTileResult(content=data.getvalue(), media_type="image/png")

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(
                f"[RasterTileError] uuid={uuid}, z={z}, x={x}, y={y} - {type(e).__name__}: {e}"
            )
            raise HTTPException(status_code=500, detail=f"Unable to render raster tile: {e}")
