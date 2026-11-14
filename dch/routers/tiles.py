import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.models import LayerInfo
from app.damapql.dch.services.layer_accessibility import VectorAccessService
from app.damapql.dch.services.layer_tiling import RasterTileService, VectorTileService
from app.damapql.utils.adapters.deps import get_minio_manager
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

dch_tiles_router = APIRouter(tags=["dch"])

_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F\-]{32,36}$")

MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"

MVT_HEADERS = {
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store, no-cache, must-revalidate",
}


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
        headers=MVT_HEADERS,
    )


@dch_tiles_router.get(
    "/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}",
    name="get_layer_mvt",
    response_class=Response,
    summary="Get Mapbox Vector Tile",
)
def get_layer_mvt(
    uuid: str,
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
    user=Depends(AuthUtils.get_optional_user),
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    mvt_bytes = VectorTileService.render_tile(
        uuid=uuid,
        tile_size=tile_size,
        z=z,
        x=x,
        y=y,
        model_name=model_name,
        table_name=table_name,
        geom_col=geom_col,
        db_key=db_key,
        resolution=resolution,
        cols=cols,
        minio_manager=minio_manager,
    )

    if mvt_bytes is None:
        return Response(status_code=204, headers=MVT_HEADERS)

    return Response(
        content=mvt_bytes,
        media_type=MVT_MEDIA_TYPE,
        headers=MVT_HEADERS,
    )


@dch_tiles_router.get("/clear_cache/")
def clear_cache(
    uuid: UUID | None = None,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Clear Redis-backed and SQLite-backed MVT cache, plus any locally cached vector object.
    """
    require_tile_manager(current_user)
    layer_uuid = uuid.hex if uuid else None

    try:
        if layer_uuid:
            info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
            VectorAccessService.evict_cached_file(info)

        cleared_count = mvt_cache.clear_cache(layer_uuid=layer_uuid)
        scope = f"layer {layer_uuid}" if layer_uuid else "all application layers"

        da_logger.info(f"MVT cache cleared | scope={scope} | keys={cleared_count}")

        return {
            "payload": {
                "layer_uuid": layer_uuid,
                "cleared_count": cleared_count,
                "scope": "layer" if layer_uuid else "application",
                "message": (
                    "Layer cache successfully cleared"
                    if layer_uuid
                    else "Application cache successfully cleared"
                ),
            }
        }

    except Exception as err:
        da_logger.exception(
            f"Failed to clear MVT cache | layer={layer_uuid or 'ALL'} | error={err}"
        )
        raise HTTPException(status_code=500, detail="Failed to clear MVT cache")