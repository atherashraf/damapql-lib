import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import LayerInfo, DBQuery
from app.damapql.dch.services.raster_tile_service import RasterTileService
from app.damapql.dch.utils.mvt_utils import MVTCache
from app.damapql.utils.adapters.db import DBBase
from digitalarzengine.utils.singletons import da_logger

from app.damapql.utils.adapters.model_utils import ModelUtils

dch_tiles_router = APIRouter(tags=["dch"])


_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F\-]{32,36}$")


def require_tile_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


def cache_table_name(uuid: str) -> str:
    """
    Returns a safe schema-qualified cache table name.
    Example: main."6f4b..._xyz"
    """
    if not _UUID_TOKEN_RE.fullmatch(uuid):
        raise HTTPException(status_code=400, detail="Invalid layer uuid")

    token = uuid.replace("-", "")
    return f'main."{token}_xyz"'


@dch_tiles_router.get(
    "/raster_tile/{uuid}/{z}/{x}/{y}/",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
@dch_tiles_router.get(
    "/raster_tile/{uuid}/{z}/{x}/{y}",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
async def get_raster_tile(uuid: str, z: int, x: int, y: int):
    tile_result = RasterTileService.render_tile(uuid=uuid, z=z, x=x, y=y)
    # Return raw binary bytes with the proper headers
    return Response(
        content=tile_result.content,
        media_type="image/png",
        headers={
            "Content-Type": "image/png",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        }
    )


@dch_tiles_router.get("/layer_mvt/{identifier}/{tile_size}/{z}/{x}/{y}")
async def get_layer_mvt(
        identifier: str,
        tile_size: int,
        z: int,
        x: int,
        y: int,
        model_name: str | None = Query(None),
        table_name: str | None = Query(None),
        geom_col: str | None = Query(None),
        db_key: str | None = Query(None),
        resolution: float = 0,
        cols: list[str] | None = Query(None),
):
    """
    Unified MVT tile endpoint supporting:
    1) LayerInfo UUID lookup (`identifier = layer_uuid`)
    2) Model Name resolution via ModelUtils (`model_name=WASAPipeModel`)
    3) Direct PostGIS table name lookup (`table_name=public.canals`)
    """
    external_engine = None
    target_table = None
    target_geom = None
    pk_sql_fragment = '"id" as pk'
    allowed_cols = set()
    layer_name = identifier
    zoom_range = [0, 30]
    feature_count: int | None = None

    # -------------------------------------------------------------
    # RESOLUTION PATH 1: Standard LayerInfo UUID
    # -------------------------------------------------------------
    info = LayerInfo.get_layer_info_by_uuid(identifier)

    if info:
        zoom_range = info.zoom_range or zoom_range
        access = info.layer_accessibility
        if not access:
            return Response(status_code=204)

        layer_total_rows = info.count_layer_rows()
        if layer_total_rows == 0:
            return Response(content=b"", media_type="application/vnd.mapbox-vector-tile")

        feature_count = layer_total_rows
        external_engine = access.get_external_engine()
        target_table = access.table_name
        target_geom = access.geometry_col
        pk_sql_fragment = info.pk_cols_as_pk
        allowed_cols = set(access.cols_name or [])
        layer_name = info.layer_name or identifier

    # -------------------------------------------------------------
    # RESOLUTION PATH 2: Dynamic SQLAlchemy Model Class Name
    # -------------------------------------------------------------
    elif model_name or hasattr(ModelUtils, "get_model_info"):
        target_model_name = model_name or identifier
        try:
            info_dict = ModelUtils.get_model_info(target_model_name)
            model_cls = info_dict["model"]

            # ModelHelpersMixin utilities
            external_engine = model_cls.get_engine()
            target_table = model_cls.__tablename__

            detected_geom, _ = model_cls.get_geom_col_name_srid()
            target_geom = geom_col or detected_geom or "geom"

            pk_fields = model_cls.get_primary_key_fields()
            if pk_fields:
                pk_sql_fragment = " || '-' || ".join([f'"{f}"' for f in pk_fields]) + " as pk"

            # Auto-detect accessible columns from SQLAlchemy table keys
            all_fields = model_cls.get_fields()
            allowed_cols = set([f for f in all_fields if f != target_geom])
            layer_name = model_cls.__name__

        except ValueError:
            # Fallback if model_name was not found and table_name was not provided
            if not table_name:
                raise HTTPException(status_code=404, detail=f"Model or Layer '{identifier}' not found")

    # -------------------------------------------------------------
    # RESOLUTION PATH 3: Direct PostGIS Table Name
    # -------------------------------------------------------------
    if not external_engine and table_name:
        try:
            target_db_key = db_key or "default"
            external_engine = DBBase.get_engine(db_key=target_db_key)
            target_table = table_name
            target_geom = geom_col or "geom"
            allowed_cols = set(cols or [])
            layer_name = identifier
        except KeyError:
            raise HTTPException(status_code=400, detail="Invalid DB connection engine")

    if not external_engine or not target_table:
        raise HTTPException(status_code=404, detail="Unable to resolve layer database target")

    # -------------------------------------------------------------
    # ZOOM RANGE VALIDATION
    # -------------------------------------------------------------
    if zoom_range and len(zoom_range) == 2:
        min_zoom, max_zoom = zoom_range
        if z < min_zoom or z > max_zoom:
            return Response(content=b"", media_type="application/vnd.mapbox-vector-tile")

    # -------------------------------------------------------------
    # COLUMN & VARIANT CACHING PIPELINE
    # -------------------------------------------------------------
    requested_cols = cols or list(allowed_cols)
    if allowed_cols:
        requested_cols = [c for c in requested_cols if c in allowed_cols]
    requested_cols = sorted(set(requested_cols))

    # Incorporate feature_count into variant hash
    variant = MVTCache.build_variant(
        cols=requested_cols,
        resolution=resolution,
        feature_count=feature_count,
        extra={"table": target_table, "model": model_name or ""}
    )

    # Cache Read
    try:
        cached = MVTCache.get_cached_tile(identifier, x, y, z, variant)
        if cached:
            return Response(content=bytes(cached), media_type="application/vnd.mapbox-vector-tile")
    except Exception as cache_err:
        da_logger.exception(f"[MVTCacheReadError] identifier={identifier} - {cache_err}")

    # Generate MVT SQL (Double-quoting all dynamic column names)
    safe_cols = [DBQuery.safe_ident(c) for c in requested_cols]
    all_cols_sql = ", ".join([pk_sql_fragment] + safe_cols) if safe_cols else pk_sql_fragment

    safe_geom = DBQuery.safe_ident(target_geom)
    safe_table = DBQuery.safe_ident(target_table)

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
            extent=tile_size,
            resolution=resolution,
        )

    # Cache Write
    if mvt_bytes:
        try:
            MVTCache.set_cached_tile(identifier, x, y, z, variant, mvt_bytes)
        except Exception as cache_err:
            da_logger.exception(f"[MVTCacheWriteError] identifier={identifier} - {cache_err}")

    return Response(
        content=mvt_bytes or b"",
        media_type="application/vnd.mapbox-vector-tile"
    )


@dch_tiles_router.delete("/clear_cache/{uuid}/")
async def clear_cache(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Protected endpoint to clear cached vector tiles for a layer.
    """
    require_tile_manager(current_user)

    if not re.fullmatch(r"[A-Fa-f0-9\-]+", uuid):
        raise HTTPException(status_code=400, detail="Invalid uuid")

    try:
        cache_engine = DBBase.get_engine(db_key="cache_db")
    except KeyError:
        raise HTTPException(status_code=500, detail="Cache database configuration missing")

    clean_uuid = uuid.replace("-", "")
    table_name = f'"{clean_uuid}_xyz"'

    with Session(cache_engine) as session:
        try:
            session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            session.commit()
            return {"payload": f"Cache table {table_name} successfully cleared"}
        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to clear cache for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Error clearing cache")