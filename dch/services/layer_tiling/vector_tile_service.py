# app/damapql/dch/services/layer_tiling/vector_tile_service.py
from __future__ import annotations

import time
from typing import Optional, Sequence

from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.models import DBQuery, LayerInfo
from app.damapql.dch.services.layer_accessibility import (
    VectorAccessService,
    VectorSourceFormat,
    VectorTarget,
)
from app.damapql.dch.utils.mvt_utils import MVTUtils
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.model_utils import ModelUtils


class VectorTileService:
    """
    Orchestrates Mapbox Vector Tile (MVT) generation across:
      - LayerInfo UUIDs (PostGIS DB, FlatGeobuf, GeoParquet, GeoPackage)
      - Registered SQLAlchemy ORM models (model_name)
      - Direct PostGIS tables (table_name + db_key)

    Handles zoom filtering, resolution scaling, MVTCache variants, and distributed locks.
    """

    @classmethod
    def render_tile(
        cls,
        *,
        uuid: str,
        tile_size: int,
        z: int,
        x: int,
        y: int,
        model_name: Optional[str] = None,
        table_name: Optional[str] = None,
        geom_col: Optional[str] = None,
        db_key: Optional[str] = None,
        resolution: float = 0,
        cols: Optional[Sequence[str]] = None,
        minio_manager: Optional[MinioManager] = None,
    ) -> Optional[bytes]:
        """
        Returns:
          - bytes: MVT protobuf payload (may be b"" for empty/out-of-zoom tiles)
          - None: LayerInfo exists without accessibility configuration (HTTP 204)
        """
        identifier = uuid
        model_cls = None
        vector_target: Optional[VectorTarget] = None

        external_engine = None
        target_table: Optional[str] = None
        target_geom: Optional[str] = None
        pk_sql_fragment = '"id" as pk'
        allowed_cols: set[str] = set()
        layer_name = identifier
        zoom_range = [0, 30]
        feature_count: Optional[int] = None
        srid: Optional[int] = None

        # -------------------------------------------------------------
        # 1. LAYERINFO UUID LOOKUP (DB, FGB, GeoParquet, GPKG)
        # -------------------------------------------------------------
        info = LayerInfo.get_layer_info_by_uuid(identifier)

        if info:
            if not info.layer_accessibility:
                return None

            vector_target = VectorAccessService.resolve(info, minio_manager=minio_manager)
            zoom_range = vector_target.zoom_range
            feature_count = vector_target.feature_count
            if feature_count == 0:
                return b""

            allowed_cols = set(vector_target.columns)
            layer_name = vector_target.layer_name
            srid = vector_target.srid

            if vector_target.source_format == VectorSourceFormat.POSTGIS:
                external_engine = vector_target.engine
                target_table = vector_target.table_name
                target_geom = vector_target.geom_col
                pk_sql_fragment = vector_target.pk_sql_fragment
            else:
                target_table = vector_target.source_uri or str(vector_target.local_path or "")
                target_geom = vector_target.geom_col

        # -------------------------------------------------------------
        # 2. SQLALCHEMY MODEL LOOKUP
        # -------------------------------------------------------------
        elif model_name:
            try:
                info_dict = ModelUtils.get_model_info(model_name)
                model_cls = info_dict["model"]
                external_engine = model_cls.get_engine()
                target_table = model_cls.__tablename__

                detected_geom, detected_srid = model_cls.get_geom_col_name_srid()
                target_geom = geom_col or detected_geom or "geom"
                srid = detected_srid

                pk_fields = model_cls.get_primary_key_fields()
                if pk_fields:
                    pk_sql_fragment = (
                        " || '-' || ".join(f'"{field}"' for field in pk_fields) + " as pk"
                    )

                all_fields = model_cls.get_fields()
                allowed_cols = {field for field in all_fields if field != target_geom}
                layer_name = model_cls.__name__
            except ValueError:
                if not table_name:
                    raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

        # -------------------------------------------------------------
        # 3. DIRECT POSTGIS TABLE LOOKUP
        # -------------------------------------------------------------
        if not vector_target and not external_engine and table_name:
            try:
                external_engine = DBBase.get_engine(db_key=db_key or "default")
                target_table = table_name
                target_geom = geom_col or "geom"
                allowed_cols = set(cols or [])
                layer_name = identifier
            except KeyError:
                raise HTTPException(status_code=400, detail="Invalid DB connection engine")

        # -------------------------------------------------------------
        # 4. VALIDATE TARGET & ZOOM RANGE
        # -------------------------------------------------------------
        is_file_vector = bool(
            vector_target and vector_target.source_format != VectorSourceFormat.POSTGIS
        )
        if not is_file_vector and (not external_engine or not target_table or not target_geom):
            raise HTTPException(status_code=404, detail="Unable to resolve layer database target")

        if zoom_range and len(zoom_range) == 2:
            min_zoom, max_zoom = zoom_range
            if z < min_zoom or z > max_zoom:
                return b""

        # -------------------------------------------------------------
        # 5. COLUMNS FILTER, RESOLUTION & CACHE LOOKUP
        # -------------------------------------------------------------
        # requested_cols = sorted(allowed_cols.intersection(cols)) if cols else sorted(allowed_cols)
        requested_cols = (
            sorted(allowed_cols.intersection(cols))
            if cols is not None
            else sorted(allowed_cols)
        )
        if resolution <= 0:
            resolution = 78271.52 / (2 ** z)

        variant = mvt_cache.build_variant(
            cols=requested_cols,
            resolution=resolution,
            tile_size=tile_size,
            feature_count=feature_count,
            extra={"table": target_table or "", "model": model_name or ""},
        )

        cached = mvt_cache.get_cached_tile(identifier, x, y, z, variant)
        if cached is not None:
            return cached

        # -------------------------------------------------------------
        # 6. DISTRIBUTED TILE LOCK & GENERATION
        # -------------------------------------------------------------
        lock_token = mvt_cache.acquire_tile_lock(identifier, x, y, z, variant)
        if lock_token is None:
            for _ in range(10):
                time.sleep(0.05)
                cached = mvt_cache.get_cached_tile(identifier, x, y, z, variant)
                if cached is not None:
                    return cached

        mvt_bytes: Optional[bytes] = None
        try:
            if is_file_vector and vector_target is not None:
                mvt_bytes = cls._generate_file_vector_tile(
                    target=vector_target,
                    x=x,
                    y=y,
                    z=z,
                    layer_name=layer_name,
                    requested_cols=requested_cols,
                )
            elif model_cls:
                mvt_bytes = MVTUtils.get_tile_bytes(
                    model_cls=model_cls,
                    x=x,
                    y=y,
                    z=z,
                    layer_name=layer_name,
                    requested_cols=requested_cols,
                    geom_col=target_geom,
                    extent=MVTUtils.MVT_EXTENT,
                    resolution=resolution,
                )
            else:
                safe_cols = [DBQuery.safe_ident(col) for col in requested_cols]
                all_cols_sql = (
                    ", ".join([pk_sql_fragment, *safe_cols])
                    if safe_cols
                    else pk_sql_fragment
                )
                safe_geom = DBQuery.safe_ident(target_geom)
                safe_table = ".".join(DBQuery.safe_ident(part) for part in target_table.split("."))
                stmt = f"SELECT {all_cols_sql}, {safe_geom} FROM {safe_table}"

                with Session(external_engine) as ext_session:
                    mvt_bytes = DBQuery.execute_query_as_mvt(
                        session=ext_session,
                        stmt=stmt,
                        z=z,
                        x=x,
                        y=y,
                        geom_col=target_geom,
                        layer_name=layer_name,
                        extent=MVTUtils.MVT_EXTENT,
                        resolution=resolution,
                        srid=srid,
                    )

            if mvt_bytes is not None:
                mvt_cache.set_cached_tile(identifier, x, y, z, variant, mvt_bytes)

        finally:
            if lock_token and lock_token != "redis-unavailable":
                mvt_cache.release_tile_lock(identifier, x, y, z, variant, lock_token)

        return mvt_bytes or b""

    @staticmethod
    def _generate_file_vector_tile(
        *,
        target: VectorTarget,
        x: int,
        y: int,
        z: int,
        layer_name: str,
        requested_cols: list[str],
    ) -> bytes:
        bbox_4326 = tuple(MVTUtils.xyz_to_extent_4326(x, y, z))
        env_3857_df = MVTUtils.xyz_to_extent_3857(x, y, z)
        tile_extent_3857 = (
            float(env_3857_df["minx"].iloc[0]),
            float(env_3857_df["miny"].iloc[0]),
            float(env_3857_df["maxx"].iloc[0]),
            float(env_3857_df["maxy"].iloc[0]),
        )

        gdf = VectorAccessService.read_gdf(
            target=target,
            bbox_4326=bbox_4326,
            requested_cols=requested_cols,
        )
        da_logger.debug(f"Read {gdf.shape[0]} rows from {target.source_uri}")
        if gdf.empty:
            return b""

        return MVTUtils.gdf_to_mvt(gdf, tile_extent_3857, name=layer_name)