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
    """
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Layer not found")

    access = info.layer_accessibility
    if not access:
        return Response(status_code=204)

    # 1) cache read
    try:
        cached = MVTCache.get_cached_tile(uuid, x, y, z)
        if cached:
            return Response(
                content=bytes(cached),
                media_type="application/vnd.mapbox-vector-tile"
            )
    except Exception as cache_err:
        da_logger.exception(
            f"[MVTCacheReadError] uuid={uuid}, z={z}, x={x}, y={y} - {cache_err}"
        )

    # 2) generate on cache miss
    try:
        external_engine = access.get_external_engine()
        if not external_engine:
            return Response(status_code=204)

        allowed_cols = set(access.cols_name or [])
        requested_cols = cols or list(allowed_cols)

        bad = [c for c in requested_cols if c not in allowed_cols]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

        select_cols = ", ".join(DBQuery.safe_ident(c) for c in requested_cols)
        geom_col = DBQuery.safe_ident(access.geometry_col)
        table = DBQuery.safe_ident(access.table_name)

        stmt = (
            f"SELECT {select_cols}, {geom_col} FROM {table}"
            if select_cols else
            f"SELECT {geom_col} FROM {table}"
        )

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

        # 3) cache write
        if mvt_bytes:
            try:
                MVTCache.set_cached_tile(uuid, x, y, z, mvt_bytes)
            except Exception as cache_err:
                da_logger.exception(
                    f"[MVTCacheWriteError] uuid={uuid}, z={z}, x={x}, y={y} - {cache_err}"
                )

        return Response(
            content=mvt_bytes or b"",
            media_type="application/vnd.mapbox-vector-tile"
        )

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"[MVTError] uuid={uuid}, z={z}, x={x}, y={y} - {e}")
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

    try:
        cache_engine = DBBase.get_engine(app_label="cache_db")
    except KeyError:
        raise HTTPException(status_code=500, detail="Cache database configuration missing")

    xyz_table = cache_table_name(uuid)

    with Session(cache_engine) as session:
        try:
            session.execute(text(f"DROP TABLE IF EXISTS {xyz_table}"))
            session.commit()
            return {"payload": f"Cache table {xyz_table} successfully cleared"}
        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to clear cache for uuid={uuid}: {e}")
            return {"payload": "error clearing cache", "detail": str(e)}