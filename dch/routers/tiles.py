import re
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.models import LayerInfo, DBQuery
from app.damapql.dch.services.raster_tile_service import RasterTileService
from app.damapql.dch.utils.mvt_utils import MVTUtils
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.deps import get_minio_manager
from app.damapql.utils.adapters.model_utils import ModelUtils
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

dch_tiles_router = APIRouter(tags=["dch"])

_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F\-]{32,36}$")


def require_tile_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(status_code=403, detail="Superuser or app_manager privileges required")


def cache_table_name(uuid: str) -> str:
    """
    Returns a safe schema-qualified cache table name.
    Example: main."6f4b..._xyz"
    """
    if not _UUID_TOKEN_RE.fullmatch(uuid):
        raise HTTPException(status_code=400, detail="Invalid layer uuid")

    token = uuid.replace("-", "")
    return f'main."{token}_xyz"'


MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

MVT_HEADERS = {
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store, no-cache, must-revalidate",  # Adjust for production caching
}


@dch_tiles_router.get(
    "/raster_tile/{uuid}/{z}/{x}/{y}",
    name="get_layer_tms",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
    summary="Get TMS Raster Tile",
)
async def get_raster_tile(
    uuid: str,
    z: int,
    x: int,
    y: int,
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    tile_result = RasterTileService.render_tile(
        uuid=uuid,
        z=z,
        x=x,
        y=y,
        minio_manager=minio_manager,
    )

    return Response(
        content=tile_result.content,
        media_type=tile_result.media_type,
        headers={
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@dch_tiles_router.get(
    "/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}",
    name="get_layer_mvt",
    response_class=Response,
    summary="Get Mapbox Vector Tile",
)
def get_layer_mvt(
    uuid: str,  # MUST match {uuid} in the path template
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
    1. LayerInfo UUID lookup
    2. SQLAlchemy model lookup
    3. Direct PostGIS table lookup
    """
    identifier = uuid  # Preserves downstream identifier naming

    model_cls = None
    external_engine = None
    target_table = None
    target_geom = None

    pk_sql_fragment = '"id" as pk'
    allowed_cols: set[str] = set()

    layer_name = identifier
    zoom_range = [0, 30]
    feature_count: int | None = None

    # -------------------------------------------------------------
    # 1. LAYERINFO UUID LOOKUP
    # -------------------------------------------------------------
    info = LayerInfo.get_layer_info_by_uuid(identifier)

    if info:
        zoom_range = info.zoom_range or zoom_range
        access = info.layer_accessibility

        if not access:
            return Response(status_code=204, headers=MVT_HEADERS)

        layer_total_rows = info.count_layer_rows()
        if layer_total_rows == 0:
            return Response(
                content=b"",
                media_type=MVT_MEDIA_TYPE,
                headers=MVT_HEADERS,
            )

        feature_count = layer_total_rows
        external_engine = access.get_external_engine()
        target_table = access.table_name
        target_geom = access.geometry_col
        pk_sql_fragment = info.pk_cols_as_pk
        allowed_cols = set(access.cols_name or [])
        layer_name = info.layer_name or identifier

    # -------------------------------------------------------------
    # 2. SQLALCHEMY MODEL LOOKUP
    # -------------------------------------------------------------
    elif model_name:
        try:
            info_dict = ModelUtils.get_model_info(model_name)
            model_cls = info_dict["model"]
            external_engine = model_cls.get_engine()
            target_table = model_cls.__tablename__

            detected_geom, _ = model_cls.get_geom_col_name_srid()
            target_geom = geom_col or detected_geom or "geom"

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
                raise HTTPException(
                    status_code=404,
                    detail=f"Model '{model_name}' not found",
                )

    # -------------------------------------------------------------
    # 3. DIRECT POSTGIS TABLE LOOKUP
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

    # -------------------------------------------------------------
    # VALIDATE TARGET
    # -------------------------------------------------------------
    if not external_engine or not target_table or not target_geom:
        raise HTTPException(status_code=404, detail="Unable to resolve layer database target")

    # -------------------------------------------------------------
    # ZOOM RANGE CHECK
    # -------------------------------------------------------------
    if zoom_range and len(zoom_range) == 2:
        min_zoom, max_zoom = zoom_range
        if z < min_zoom or z > max_zoom:
            return Response(content=b"", media_type=MVT_MEDIA_TYPE, headers=MVT_HEADERS)

    # -------------------------------------------------------------
    # COLUMNS & RESOLUTION
    # -------------------------------------------------------------
    requested_cols = sorted(allowed_cols)
    da_logger.debug(f"Requested columns: {requested_cols}")

    if resolution <= 0:
        resolution = 78271.52 / (2 ** z)

    # -------------------------------------------------------------
    # CACHE VARIANT & LOOKUP
    # -------------------------------------------------------------
    variant = mvt_cache.build_variant(
        cols=requested_cols,
        resolution=resolution,
        tile_size=tile_size,
        feature_count=feature_count,
        extra={"table": target_table, "model": model_name or ""},
    )

    cached = mvt_cache.get_cached_tile(identifier, x, y, z, variant)
    if cached is not None:
        return Response(content=cached, media_type=MVT_MEDIA_TYPE, headers=MVT_HEADERS)

    # -------------------------------------------------------------
    # DISTRIBUTED TILE LOCK & RECHECK
    # -------------------------------------------------------------
    lock_token = mvt_cache.acquire_tile_lock(identifier, x, y, z, variant)

    if lock_token is None:
        for _ in range(10):
            time.sleep(0.1)
            cached = mvt_cache.get_cached_tile(identifier, x, y, z, variant)
            if cached is not None:
                return Response(content=cached, media_type=MVT_MEDIA_TYPE, headers=MVT_HEADERS)

    # -------------------------------------------------------------
    # TILE GENERATION
    # -------------------------------------------------------------
    mvt_bytes: bytes | None = None

    try:
        if model_cls:
            mvt_bytes = MVTUtils.get_tile_bytes(
                model_cls=model_cls,
                x=x,
                y=y,
                z=z,
                layer_name=layer_name,
                requested_cols=requested_cols,
                geom_col=target_geom,
                extent=tile_size,
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

        # Cache valid result (empty tiles cached to avoid redundant expensive DB hits)
        if mvt_bytes is not None:
            mvt_cache.set_cached_tile(identifier, x, y, z, variant, mvt_bytes)

    finally:
        if lock_token and lock_token != "redis-unavailable":
            mvt_cache.release_tile_lock(identifier, x, y, z, variant, lock_token)

    return Response(
        content=mvt_bytes or b"",
        media_type=MVT_MEDIA_TYPE,
        headers=MVT_HEADERS,
    )


@dch_tiles_router.get("/clear_cache/")
def clear_cache(uuid: UUID | None = None, current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Clear Redis-backed MVT cache.
    """
    require_tile_manager(current_user)
    layer_uuid = str(uuid) if uuid else None

    try:
        cleared_count = mvt_cache.clear_cache(layer_uuid=layer_uuid)
        scope = f"layer {layer_uuid}" if layer_uuid else "all application layers"

        da_logger.info(f"MVT Redis cache cleared | scope={scope} | keys={cleared_count}")

        return {"payload": {"layer_uuid": layer_uuid, "cleared_count": cleared_count,
            "scope": "layer" if layer_uuid else "application", "message": (
                "Layer cache successfully cleared" if layer_uuid else "Application cache successfully cleared"), }}

    except Exception as err:
        da_logger.exception(f"Failed to clear MVT Redis cache | layer={layer_uuid or 'ALL'} | error={err}")
        raise HTTPException(status_code=500, detail="Failed to clear MVT cache")
