from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.dtypes import UploadRasterResponse
from app.damapql.dch.services.raster_ingest_service import RasterIngestService
from app.damapql.dch.services.raster_stats_service import RasterStatsService
from utils.adapters.deps import get_sync_session

dch_raster_router = APIRouter(tags=["dch"])

def require_raster_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


@dch_raster_router.get("/raster_data_model_types", tags=["Raster Info"])
async def get_data_model_types():
    """
    Public endpoint to list raster model types.
    """
    return [{"key": m.name, "value": m.value} for m in ModelType]


@dch_raster_router.post(
    "/add_raster_layer_info",
    response_model=UploadRasterResponse,
    summary="Upload and Register Raster Layer",
    description="Uploads a raster file, converts it to COG if needed, and registers it as a layer."
)
async def upload_raster_layer(
    title: str = Form(...),
    categoryId: int = Form(...),
    temporal: str = Form(...),
    data_model_type: str = Form(...),
    rasterFile: UploadFile = File(...),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Uploads a raster file, converts it to COG if needed,
    stores it under MEDIA_DIR/APP_NAME/category/main/sub,
    and registers metadata in LayerInfo and LayerAccessibility.
    """
    require_raster_manager(current_user)

    result = RasterIngestService.ingest_upload(
        session,
        title=title,
        category_id=categoryId,
        temporal=temporal,
        data_model_type=data_model_type,
        raster_file=rasterFile,
        uploaded_by=current_user.id,
    )

    return {
        "uuid": result.uuid,
        "msg": "Raster uploaded and LayerInfo created successfully.",
        "path": result.cog_path,
    }


@dch_raster_router.get("/get_pixel_value/{uuid}/{long}/{lat}/")
async def get_pixel_value(uuid: str, long: float, lat: float):
    return RasterStatsService.get_pixel_value(uuid, long, lat)


@dch_raster_router.get("/get_raster_info/{uuid}/")
def get_raster_info(uuid: str):
    result = RasterStatsService.get_raster_info(uuid)
    return JSONResponse(content={"payload": result.payload})