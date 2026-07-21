import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import  text
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import LayerInfo, DBQuery
from app.damapql.dch.services.raster_tile_service import RasterTileService
from app.damapql.dch.utils.mvt_utils import MVTCache
from app.damapql.utils.adapters.db import DBBase
from digitalarzengine.utils.singletons import da_logger

dch_tiles_router = APIRouter(tags=["dch"])


_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


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
    result = RasterTileService.render_tile(uuid=uuid, z=z, x=x, y=y)
    return Response(content=result.content, media_type=result.media_type)


@dch_tiles_router.get("/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}")
async def get_layer_mvt(
        uuid: str,
        tile_size: int,
        z: int,
        x: int,
        y: int,
        resolution: float = 0,
        cols: list[str] | None = Query(None),
):
    """
    Public endpoint to serve Mapbox Vector Tiles (MVT) with per-layer DB cache.
    Optimized to minimize DB property lookups and reuse PK logic.
    Enforces zoom level boundaries before hitting cache or generating tiles.
    """
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Layer not found")

    # 👇 1) ZOOM RANGE VALIDATION
    # Expects info.zoom_range to be a list, e.g., [14, 30]
    if info.zoom_range and isinstance(info.zoom_range, list) and len(info.zoom_range) == 2:
        min_zoom, max_zoom = info.zoom_range

        if z < min_zoom or z > max_zoom:
            da_logger.debug(
                f"[MVTZoomSkip] Requested z={z} is outside allowed range [{min_zoom}, {max_zoom}] for uuid={uuid}"
            )
            # Return a standard empty MVT response so the frontend map doesn't choke or throw 400s
            return Response(
                content=b"",
                media_type="application/vnd.mapbox-vector-tile"
            )

    access = info.layer_accessibility
    if not access:
        return Response(status_code=204)

    # Check row count before expensive engine connection
    if info.count_layer_rows() == 0:
        return Response(
            content=b"",
            media_type="application/vnd.mapbox-vector-tile"
        )

    try:
        external_engine = access.get_external_engine()
        if not external_engine:
            return Response(status_code=204)

        # --- OPTIMIZATION: Fetch PK and Column data once ---
        pk_list = access.pk_cols or []
        pk_sql_fragment = info.pk_cols_as_pk  # "col1 || '-' || col2 as pk"

        allowed_cols = set(access.cols_name or [])
        requested_cols = cols or list(allowed_cols)
        requested_cols = sorted(set(requested_cols))

        # Validate requested columns
        bad = [c for c in requested_cols if c not in allowed_cols]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

        # Build variant using PK list to ensure cache uniqueness
        variant = MVTCache.build_variant(
            cols=requested_cols,
            resolution=resolution,
            extra={"pk_cols": pk_list}
        )

        da_logger.debug(
            f"[MVTRequest] uuid={uuid}, z={z}, x={x}, y={y}, variant={variant}"
        )

        # 2) Cache Read
        try:
            cached = MVTCache.get_cached_tile(uuid, x, y, z, variant)
            if cached:
                da_logger.debug(f"[MVTCacheHit] uuid={uuid}, variant={variant}")
                return Response(
                    content=bytes(cached),
                    media_type="application/vnd.mapbox-vector-tile"
                )
        except Exception as cache_err:
            da_logger.exception(f"[MVTCacheReadError] uuid={uuid} - {cache_err}")

        # 3) Generate on cache miss
        # Prepare safe column identifiers
        safe_cols = [DBQuery.safe_ident(c) for c in requested_cols]

        # Merge the pre-computed PK fragment with other columns
        all_cols_sql = ", ".join([pk_sql_fragment] + safe_cols)

        geom_col = DBQuery.safe_ident(access.geometry_col)
        table = DBQuery.safe_ident(access.table_name)

        stmt = f"SELECT {all_cols_sql}, {geom_col} FROM {table}"

        da_logger.debug(f"[MVTGenerate] uuid={uuid}, stmt={stmt}")

        with Session(external_engine) as ext_session:
            mvt_bytes = DBQuery.execute_query_as_mvt(
                session=ext_session,
                stmt=stmt,
                z=z,
                x=x,
                y=y,
                geom_col=access.geometry_col,
                layer_name=info.layer_name or "layer",
                extent=tile_size,
                resolution=resolution,
            )

        # 4) Cache Write
        if mvt_bytes:
            try:
                MVTCache.set_cached_tile(uuid, x, y, z, variant, mvt_bytes)
                da_logger.debug(f"[MVTCacheWrite] uuid={uuid}, variant={variant}, size={len(mvt_bytes)}")
            except Exception as cache_err:
                da_logger.exception(f"[MVTCacheWriteError] uuid={uuid} - {cache_err}")
        else:
            da_logger.debug(f"[MVTEmptyTile] uuid={uuid}, variant={variant}")

        return Response(
            content=mvt_bytes or b"",
            media_type="application/vnd.mapbox-vector-tile"
        )

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"[MVTError] uuid={uuid} - {e}")
        return Response(status_code=500)




@dch_tiles_router.delete("/clear_cache/{uuid}/")
async def clear_cache(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Protected endpoint to clear cached vector tiles for a layer.
    """
    require_tile_manager(current_user)

    if not re.fullmatch(r"[A-Fa-f0-9]+", uuid):
        raise HTTPException(status_code=400, detail="Invalid uuid")

    try:
        cache_engine = DBBase.get_engine(db_key="cache_db")
    except KeyError:
        raise HTTPException(status_code=500, detail="Cache database configuration missing")

    table_name = f'"{uuid}_xyz_v2"'

    with Session(cache_engine) as session:
        try:
            session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            session.commit()
            return {"payload": f"Cache table {table_name} successfully cleared"}
        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to clear cache for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Error clearing cache")