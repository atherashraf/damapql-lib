import os
from dataclasses import dataclass


from fastapi import HTTPException

from app.damapql.dch.models import LayerInfo, AccessibilityType, DataModel
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.utils.singletons import da_logger


@dataclass
class RasterTileResult:
    content: bytes
    media_type: str = "image/png"


class RasterTileService:
    DEFAULT_TILE_SIZE = 256

    @staticmethod
    def get_layer(uuid: str) -> LayerInfo:
        info = LayerInfo.get_layer_info_by_uuid(uuid)
        if info is None or not info.data_model or info.data_model.code != DataModel.R.name:
            raise HTTPException(status_code=404, detail="Layer not found or not raster")
        return info

    @staticmethod
    def open_cog(info: LayerInfo) -> COGRaster:
        access = info.layer_accessibility
        if not access or not access.accessibility_type:
            raise HTTPException(status_code=404, detail="Layer accessibility not found")

        if access.accessibility_type.code != AccessibilityType.File.name:
            raise HTTPException(status_code=400, detail="Raster layer is not file based")

        file_path = access.file_path
        if not file_path:
            raise HTTPException(status_code=404, detail="Raster path missing in params")

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Raster file not found")

        # return COGRaster.open_from_local(file_path)
        return COGRaster.open_cog(file_path)

    @classmethod
    def render_tile(
        cls,
        *,
        uuid: str,
        z: int,
        x: int,
        y: int,
        tile_size: int | None = None,
    ) -> RasterTileResult:
        tile_size = tile_size or cls.DEFAULT_TILE_SIZE
        info = cls.get_layer(uuid)

        try:
            cog_raster = cls.open_cog(info)
            color_map = COGRaster.create_color_map(info.layer_styling) if info.layer_styling else None
            data = cog_raster.read_tile_as_png(x, y, z, color_map=color_map, tile_size=tile_size)
            return RasterTileResult(content=data.getvalue())

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(f"[RasterTileError] uuid={uuid}, z={z}, x={x}, y={y} - {e}")
            empty_tile = COGRaster.create_empty_image(tile_size, tile_size)
            return RasterTileResult(content=empty_tile.getvalue())