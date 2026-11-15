import json
import os
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from starlette.status import HTTP_400_BAD_REQUEST

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.api_enum import ModelType
from app.damapql.dch.dtypes import UploadRasterResponse
from app.damapql.dch.models import LayerInfo, TempLayerInfo
from app.damapql.dch.routers.temp_layer_router import _resolve_cached_artifact
from app.damapql.dch.services.ingestion.raster_ingest_service import RasterIngestService
from app.damapql.dch.services.raster_stats_service import RasterStatsService
from app.damapql.utils.adapters.deps import get_sync_session, get_minio_manager
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from settings import MEDIA_DIR

dch_raster_router = APIRouter(tags=["dch"])


def require_raster_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(status_code=403, detail="Superuser or app_manager privileges required")


@dch_raster_router.get("/raster_data_model_types/", tags=["Raster Info"])
async def get_data_model_types():
    """
    Public endpoint to list raster model types.
    """
    return [{"key": m.name, "value": m.value} for m in ModelType]




@dch_raster_router.post(
    "/add_raster_layer_info/",
    response_model=UploadRasterResponse,
    summary="Register Raster Layer (Upload or Existing Path)",
    description=(
        "Registers a raster layer either by uploading a new file "
        "or by referencing an existing local/MinIO raster."
    ),
)
async def add_raster_layer_info(
    title: str = Form(...),
    categoryId: int = Form(...),
    temporal: str | None = Form(None),
    data_model_type: str | None = Form(None),
    symbology: str | None = Form(None),
    # ------------------------------------------------------------
    # Ingestion mode
    # ------------------------------------------------------------
    isExisting: bool = Form(False),
    existingStorage: Literal["local", "minio"] | None = Form(None),
    # ------------------------------------------------------------
    # Input
    # ------------------------------------------------------------
    rasterFile: UploadFile | None = File(None),
    rasterFilePath: str | None = Form(None),
    # ------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: MinioManager | None = Depends(get_minio_manager),
):
    require_raster_manager(current_user)

    # ============================================================
    # EXISTING RASTER
    # ============================================================
    if isExisting:
        if not rasterFilePath:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="`rasterFilePath` is required when `isExisting` is True.",
            )
        if existingStorage not in ("local", "minio"):
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="`existingStorage` must be either 'local' or 'minio' when `isExisting` is True.",
            )

        if existingStorage == "minio":
            if minio_manager is None:
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail="MinIO storage was selected, but MinIO is not available.",
                )
            source_minio_manager = minio_manager
        else:
            source_minio_manager = None

        result = RasterIngestService.ingest_from_path(
            session=session,
            title=title,
            category_id=categoryId,
            raster_path=rasterFilePath,
            temporal=temporal,
            model_type=data_model_type,
            uploaded_by=current_user.id,
            minio_manager=source_minio_manager,
        )

        msg = (
            "Raster LayerInfo created successfully "
            f"from existing {existingStorage} raster."
        )

    # ============================================================
    # NEW RASTER UPLOAD
    # ============================================================
    else:
        if rasterFile is None:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail="`rasterFile` is required when creating a new raster layer.",
            )

        if temporal is None:
            temporal = date.today().isoformat()

        result = RasterIngestService.ingest_upload(
            session=session,
            title=title,
            category_id=categoryId,
            temporal=temporal,
            model_type=data_model_type,
            raster_file=rasterFile,
            uploaded_by=current_user.id,
            minio_manager=minio_manager,
        )

        msg = "Raster uploaded and LayerInfo created successfully."

    # ============================================================
    # APPLY INITIAL SYMBOLOGY (IF PROVIDED)
    # ============================================================
    da_logger.info(f"Applying initial symbology for raster {result.uuid}", symbology)
    if symbology:
        try:
            RasterStatsService.apply_initial_symbology(
                uuid=result.uuid,
                symbology=symbology,
                minio_manager=minio_manager,
                session=session,
            )
        except Exception as style_err:
            da_logger.warning(
                f"Failed to apply initial symbology for raster {result.uuid}: {style_err}"
            )

    # ============================================================
    # RESPONSE
    # ============================================================
    return {"uuid": result.uuid, "msg": msg}


@dch_raster_router.get("/get_pixel_value/{uuid}/{long}/{lat}/")
async def get_pixel_value(uuid: str, long: float, lat: float):
    return RasterStatsService.get_pixel_value(uuid, long, lat)



@dch_raster_router.get("/get_raster_info/{uuid}/")
def get_raster_info(
    uuid: str,
    is_temp: bool = Query(False),
    session: Session = Depends(get_sync_session),
    minio_manager: MinioManager | None = Depends(get_minio_manager),
):
    if is_temp:
        record = session.query(TempLayerInfo).filter(TempLayerInfo.uuid == uuid).first()
        if not record:
            raise HTTPException(status_code=404, detail="Temporary raster layer not found")

        params = record.storage_params or {}
        return JSONResponse(
            content={
                "payload": {
                    "uuid": record.uuid,
                    "title": record.title,
                    "band_count": params.get("band_count", 1),
                    "stats": params.get("stats", []),
                    "extent_3857": record.extent_3857,
                    "style": record.layer_styling,
                }
            }
        )

    result = RasterStatsService.get_raster_info(
        uuid=uuid,
        minio_manager=minio_manager,
        session=session,
    )
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
async def get_raster_pixel_value(
    uuid: str,
    lon: float,
    lat: float,
    is_temp: bool = Query(False),
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    try:
        cog_raster = None
        layer_styling = None

        # =====================================================================
        # BRANCH 1: Temporary Raster Layer (TempLayerInfo)
        # =====================================================================
        if is_temp:
            with TempLayerInfo.managed_session() as session:
                temp_info = session.query(TempLayerInfo).filter_by(uuid=uuid).one_or_none()

                if temp_info is None:
                    return {
                        "success": False,
                        "message": "Temporary raster layer not found",
                        "uuid": uuid,
                        "lon": lon,
                        "lat": lat,
                        "value": None,
                    }

                layer_styling = temp_info.layer_styling
                raster_path = _resolve_cached_artifact(
                    temp_info,
                    minio_manager,
                    default_ext="_cog.tif",
                )
                cog_raster = COGRaster.open_from_local(raster_path)

        # =====================================================================
        # BRANCH 2: Permanent Catalog Raster Layer (LayerInfo)
        # =====================================================================
        else:
            with LayerInfo.managed_session() as session:
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

                layer_styling = info.layer_styling
                params = info.layer_accessibility.accessibility_params or {}

                if info.layer_accessibility.is_minio:
                    cog_raster = COGRaster.open_from_minio(
                        params,
                        minio_manager=minio_manager,
                        expires_minutes=30,
                    )
                else:
                    file_path = params.file_path
                    if not file_path:
                        return {
                            "success": False,
                            "message": "Raster storage parameters missing in database",
                            "uuid": uuid,
                            "lon": lon,
                            "lat": lat,
                            "value": None,
                        }

                    if not os.path.isabs(file_path):
                        file_path = os.path.join(MEDIA_DIR, file_path.lstrip("/\\"))

                    cog_raster = COGRaster.open_cog(file_path)

        if cog_raster is None:
            return {
                "success": False,
                "message": "Unable to open raster dataset",
                "uuid": uuid,
                "lon": lon,
                "lat": lat,
                "value": None,
            }

        # =====================================================================
        # SHARED PIXEL SAMPLING & RESPONSE FORMATTING
        # =====================================================================
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

        raw_value = value
        if isinstance(value, list) and len(value):
            raw_value = value[0]

        try:
            raw_value = float(raw_value)
        except Exception:
            pass

        # Check explicit raster NoData and extreme float sentinel values
        is_nodata = False
        if isinstance(raw_value, float):
            if raw_value < -1.0e30 or (
                cog_raster.nodata is not None
                and not (cog_raster.nodata != cog_raster.nodata)  # not NaN
                and abs(raw_value - float(cog_raster.nodata)) < 1e-6
            ) or (raw_value != raw_value):  # NaN check
                is_nodata = True

        if is_nodata:
            return {
                "success": False,
                "message": "NoData pixel",
                "uuid": uuid,
                "lon": lon,
                "lat": lat,
                "value": None,
                "label": None,
            }

        label = get_raster_style_label(raw_value, layer_styling)

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
            f"[RasterPixelError] uuid={uuid}, lon={lon}, lat={lat}, is_temp={is_temp} — {e}"
        )
        return {
            "success": False,
            "message": str(e),
            "uuid": uuid,
            "lon": lon,
            "lat": lat,
            "value": None,
        }
