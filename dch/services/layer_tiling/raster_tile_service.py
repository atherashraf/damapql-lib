# app/damapql/dch/services/layer_tiling/raster_tile_service.py
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException

from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.models import DataModel, LayerInfo
from app.damapql.dch.services.layer_accessibility.raster_access_service import RasterAccessService


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
    def _build_cache_variant(
        info: LayerInfo,
        tile_size: int,
        custom_color_map: Any,
    ) -> str:
        access = info.layer_accessibility
        raw_params = (access.params or {}) if access else {}
        source_key = (
            raw_params.get("object_name")
            or raw_params.get("file_path")
            or ""
        )
        style_payload = custom_color_map if custom_color_map is not None else (info.layer_styling or {})
        style_hash = hashlib.md5(
            json.dumps(style_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]

        return mvt_cache.build_variant(
            cols=[],
            resolution=0,
            tile_size=tile_size,
            feature_count=None,
            extra={
                "kind": "raster_png",
                "bucket": raw_params.get("bucket", ""),
                "source": source_key,
                "style": style_hash,
            },
        )

    @classmethod
    def render_tile(
        cls,
        *,
        uuid: str,
        z: int,
        x: int,
        y: int,
        minio_manager: Optional[MinioManager] = None,
        tile_size: Optional[int] = None,
        custom_color_map: Any = None,
        use_cache: bool = True,
    ) -> RasterTileResult:
        tile_size = tile_size or cls.DEFAULT_TILE_SIZE

        try:
            info = cls.get_layer(uuid)

            if info.zoom_range and len(info.zoom_range) == 2:
                min_zoom, max_zoom = info.zoom_range
                if z < min_zoom or z > max_zoom:
                    return RasterTileResult(content=b"", media_type="image/png")

            variant: Optional[str] = None
            lock_token: Optional[str] = None
            if use_cache:
                variant = cls._build_cache_variant(info, tile_size, custom_color_map)
                cached = mvt_cache.get_cached_tile(uuid, x, y, z, variant)
                if cached is not None:
                    return RasterTileResult(content=cached, media_type="image/png")

                lock_token = mvt_cache.acquire_tile_lock(uuid, x, y, z, variant)
                if lock_token is None:
                    for _ in range(10):
                        time.sleep(0.05)
                        cached = mvt_cache.get_cached_tile(uuid, x, y, z, variant)
                        if cached is not None:
                            return RasterTileResult(content=cached, media_type="image/png")

            try:
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
                png_bytes = data.getvalue()

                if use_cache and variant is not None:
                    mvt_cache.set_cached_tile(uuid, x, y, z, variant, png_bytes)

                da_logger.debug(f"[RasterTile] SUCCESS uuid={uuid}, z={z}, x={x}, y={y}")
                return RasterTileResult(content=png_bytes, media_type="image/png")
            finally:
                if lock_token and lock_token != "redis-unavailable":
                    mvt_cache.release_tile_lock(uuid, x, y, z, variant, lock_token)

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(
                f"[RasterTileError] uuid={uuid}, z={z}, x={x}, y={y} - {type(e).__name__}: {e}"
            )
            raise HTTPException(status_code=500, detail=f"Unable to render raster tile: {e}")