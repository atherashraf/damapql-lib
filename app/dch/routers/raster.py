import os
import shutil
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse
from utils.adapters.deps import get_sync_session

from starlette.responses import JSONResponse

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
from app.dch.dtypes import UploadRasterResponse
from app.dch.models import (
    AccessibilityType,
    COGRaster,
    DataModel,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
    ModelType,
    RioRaster,
    BandProcess,
)
from app.dch.utils.dch_utils import DCHUtils
from settings import APP_NAME, MEDIA_DIR
from utils.adapters.deps import get_sync_session
from utils.singletons import da_logger

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

    category = session.get(LayerCategory, categoryId)
    if not category:
        raise HTTPException(status_code=400, detail="Invalid category ID")

    folder_path = os.path.join(
        MEDIA_DIR,
        APP_NAME,
        category.main,
        category.sub or "general"
    )
    os.makedirs(folder_path, exist_ok=True)

    uuid_str = DCHUtils.get_uuid1()
    file_ext = os.path.splitext(rasterFile.filename)[1]
    unique_name = f"{title}_{uuid_str}_{temporal}{file_ext}"
    raster_path = os.path.join(folder_path, unique_name)

    with open(raster_path, "wb") as f:
        shutil.copyfileobj(rasterFile.file, f)

    try:
        _ = COGRaster.open_from_local(raster_path)
        cog_path = raster_path
    except Exception:
        cog_path = raster_path.replace(file_ext, f"_cog{file_ext}")
        cog_path = COGRaster.create_cog(raster_path, des_path=cog_path)

        try:
            os.remove(raster_path)
        except Exception as e:
            da_logger.warning(f"Failed to delete original raster after COG conversion: {e}")

        raster_path = cog_path

    raster = RioRaster(cog_path)

    relative_path = os.path.relpath(cog_path, MEDIA_DIR)
    rad_res = raster.get_radiometric_resolution()

    access = LayerAccessibility(
        accessibility_type=AccessibilityType.File.name,
        params={
            "file_path": relative_path,
            "is_path_media_rel": True,
            "spectral_resolution": raster.get_spectral_resolution(),
            "spatial_resolution": raster.get_spatial_resolution(),
            "radiometric_resolution": f"{rad_res}",
        },
    )
    session.add(access)
    session.commit()
    session.refresh(access)

    try:
        temporal_date = datetime.strptime(temporal, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.")

    file_name = os.path.basename(cog_path)
    layer_name = os.path.splitext(file_name)[0]
    envelop = raster.get_envelop()

    layer_info = LayerInfo(
        uuid=uuid_str,
        app_label="dch",
        title=title,
        layer_name=layer_name,
        srid=raster.get_raster_srid(),
        extent=envelop.total_bounds.tolist(),
        extent_3857=envelop.to_crs(epsg=3857).total_bounds.tolist(),
        data_model=DataModel.R.name,
        model_type=data_model_type,
        geom_type=[],
        uploaded_by=current_user.id,
        temporal_res=temporal_date,
        category=category.id,
        layer_access_id=access.id,
        quality_level=None,
        format="COG",
    )

    session.add(layer_info)
    session.commit()
    session.refresh(layer_info)

    return {
        "uuid": layer_info.uuid,
        "msg": "Raster uploaded and LayerInfo created successfully.",
        "path": raster_path,
    }


@dch_raster_router.get("/get_pixel_value/{uuid}/{long}/{lat}/")
async def get_pixel_value(uuid: str, long: float, lat: float):
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info:
        return Response(status_code=204)

    if info.data_model and info.data_model.code == DataModel.R.name:
        try:
            cog_raster = info.to_cog()
            if cog_raster:
                data = cog_raster.get_pixel_value_at_long_lat(long, lat)
                return {"payload": data}

            da_logger.error(f"Raster file not found for uuid={uuid}")
        except Exception as e:
            da_logger.exception(f"Error extracting pixel value for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Internal processing error")

    return Response(status_code=204)

@dch_raster_router.get("/get_raster_info/{uuid}/")
def get_raster_info(uuid: str):
    """
    Public endpoint to retrieve raster band information and summary stats.
    """
    try:
        info = LayerInfo.get_layer_info_by_uuid(uuid)

        if not info:
            raise HTTPException(status_code=404, detail="Layer metadata not found")

        if not info.data_model or info.data_model.code != DataModel.R.name:
            raise HTTPException(status_code=400, detail="Requested layer is not a raster model")

        access = info.layer_accessibility
        if access.accessibility_type.code == AccessibilityType.File.name:
            fp = access.file_path

            if not os.path.exists(fp):
                da_logger.error(f"Raster file missing at: {fp}")
                return Response(status_code=204)

            raster = RioRaster(fp)
            band_count = raster.get_spectral_resolution()
            bands_info = {}

            for band_index in range(1, band_count + 1):
                data_array = raster.get_data_array(band_index)
                nodata = raster.get_nodata_value()
                bands_info[band_index] = BandProcess.get_summary_data(data_array, nodata)

            res = {
                "bandCount": band_count,
                "bandsInfo": bands_info,
                "driver": raster.get_driver_name(),
                "crs": str(raster.get_crs()),
            }

            return JSONResponse(content={"payload": res})

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Error extracting raster info for uuid={uuid}: {e}")
        return Response(status_code=500)

    return Response(status_code=204)