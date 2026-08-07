import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.api_enum import ModelType
from app.damapql.dch.models import LayerInfo
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.services.raster_stats_service import RasterStatsService
from app.damapql.settings import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION_NAME
from app.damapql.utils.adapters.deps import get_sync_session
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.s3_manager import S3Manager
from digitalarzengine.utils.singletons import da_logger
from settings import MEDIA_DIR

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


# @dch_raster_router.post(
#     "/add_raster_layer_info",
#     response_model=UploadRasterResponse,
#     summary="Upload and Register Raster Layer",
#     description="Uploads a raster file, converts it to COG if needed, and registers it as a layer."
# )
# async def upload_raster_layer(
#     title: str = Form(...),
#     categoryId: int = Form(...),
#     temporal: str = Form(...),
#     data_model_type: str = Form(...),
#     rasterFile: UploadFile = File(...),
#     session: Session = Depends(get_sync_session),
#     current_user: UserInfo = Depends(AuthUtils.get_current_user),
# ):
#     """
#     Uploads a raster file, converts it to COG if needed,
#     stores it under MEDIA_DIR/APP_NAME/category/main/sub,
#     and registers metadata in LayerInfo and LayerAccessibility.
#     """
#     require_raster_manager(current_user)
#
#     result = RasterIngestService.ingest_upload(
#         session,
#         title=title,
#         category_id=categoryId,
#         temporal=temporal,
#         data_model_type=data_model_type,
#         raster_file=rasterFile,
#         uploaded_by=current_user.id,
#     )
#
#     return {
#         "uuid": result.uuid,
#         "msg": "Raster uploaded and LayerInfo created successfully.",
#         "path": result.cog_path,
#     }

@dch_raster_router.post(
    "/add_raster_layer_info_from_path",
    summary="Register Raster Layer From Existing Path",
    description="Registers an already existing raster file as a layer."
)
async def add_raster_layer_info_from_path(
    title: str = Form(...),
    categoryId: int = Form(...),
    rasterPath: str = Form(...),
    temporal: str = Form(None),
    data_model_type: str = Form(None),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_raster_manager(current_user)

    try:
        service = LayerInfoService()
        obj, is_created = service.save_raster_layer_info(
            db=session,
            title=title,
            raster_path=rasterPath,
            layer_category_id=categoryId,
            user=current_user,
            temporal=temporal,
            data_model_type=data_model_type,
            overwrite=False,
        )

        session.commit()

        return {
            "uuid": obj.uuid,
            "msg": "Raster LayerInfo created successfully." if is_created else "Raster layer already exists.",
            "path": obj.extra_metadata.get("raster_path") if obj.extra_metadata else rasterPath,
        }

    except Exception as e:
        session.rollback()
        da_logger.exception(f"Failed to register raster from path: {e}")
        raise HTTPException(status_code=500, detail="Failed to register raster from path")


@dch_raster_router.get("/get_pixel_value/{uuid}/{long}/{lat}/")
async def get_pixel_value(uuid: str, long: float, lat: float):
    return RasterStatsService.get_pixel_value(uuid, long, lat)


@dch_raster_router.get("/get_raster_info/{uuid}/")
def get_raster_info(uuid: str):
    result = RasterStatsService.get_raster_info(uuid)
    return JSONResponse(content={"payload": result.payload})

def get_raster_style_label(value, layer_styling):
    if value is None or layer_styling is None:
        return None

    try:
        value = int(float(value))

        if value < 0:
            return None

        labels = layer_styling.get("labels", [])

        if value < len(labels):
            label = labels[value]

            if label is not None and str(label).strip() != "":
                return label

        return None

    except Exception as e:
        da_logger.warning(f"Failed to get raster style label: {e}")
        return None

@dch_raster_router.get("/raster_pixel/{uuid}")
async def get_raster_pixel_value(uuid: str, lon: float, lat: float):

    with LayerInfo.managed_session() as session:
        try:
            info = session.query(LayerInfo).filter_by(uuid=uuid).one_or_none()

            if info is None or info.data_model not in ["R", "Raster"]:
                return {
                    "success": False,
                    "message": "Raster layer not found",
                    "uuid": uuid,
                    "lon": lon,
                    "lat": lat,
                    "value": None,
                }

            file_path = info.layer_accessibility.file_path
            cog_raster = COGRaster.open_cog(file_path)

            pixel = cog_raster.get_pixel_value_at_long_lat(lon, lat)

            if pixel is None:
                return {
                    "success": False,
                    "message": "No pixel value found at this location",
                    "uuid": uuid,
                    "lon": lon,
                    "lat": lat,
                    "value": None,
                }

            # rio-tiler point() may return object/dict-like result depending version
            value = pixel

            try:
                if hasattr(pixel, "data"):
                    value = pixel.data.tolist()
                elif isinstance(pixel, dict):
                    value = pixel
                elif hasattr(pixel, "tolist"):
                    value = pixel.tolist()
            except Exception:
                value = str(pixel)

            # return {
            #     "success": True,
            #     "uuid": uuid,
            #     "lon": lon,
            #     "lat": lat,
            #     "value": value,
            # }
            raw_value = value

            if isinstance(value, list) and len(value):
                raw_value = value[0]

            try:
                raw_value = float(raw_value)
            except Exception:
                pass

            if isinstance(raw_value, float) and raw_value < -1.0e30:
                return {
                    "success": False,
                    "message": "NoData pixel",
                    "uuid": uuid,
                    "lon": lon,
                    "lat": lat,
                    "value": None,
                    "label": None,
                }


            label = get_raster_style_label(raw_value, info.layer_styling)

            return {
                "success": True,
                "uuid": uuid,
                "lon": lon,
                "lat": lat,
                "value": raw_value,
                "raw_value": value,
                "label": label,
                "styled_value": {
                    "value": raw_value,
                    "label": label,
                },
            }

        except Exception as e:
            da_logger.exception(
                f"[RasterPixelError] uuid={uuid}, lon={lon}, lat={lat} — {e}"
            )

            return {
                "success": False,
                "message": str(e),
                "uuid": uuid,
                "lon": lon,
                "lat": lat,
                "value": None,
            }

