# app/damapql/dch/routers/temp_layer_router.py
from __future__ import annotations

import gzip
import json
import os
import tempfile
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

import geopandas as gpd
import mercantile
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile, status, File, Body
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import TempLayerInfo
from app.damapql.dch.schemas.geojson import GeoJSON
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.mvt_utils import MVTUtils
from app.damapql.settings import MEDIA_BASE, MINIO_BUCKET_NAME
from app.damapql.utils.adapters.deps import get_minio_manager, get_sync_session
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

dch_temp_router = APIRouter(prefix="/temp_layers", tags=["Temporary Layers"])

TEMP_FGB_CACHE_DIR = os.path.join(tempfile.gettempdir(), "dch_temp_fgb_cache")
os.makedirs(TEMP_FGB_CACHE_DIR, exist_ok=True)


def _resolve_cached_fgb(
    layer: TempLayerInfo,
    minio_manager: Optional[MinioManager],
) -> str:
    """Ensures a local copy of the FlatGeobuf file exists for fast spatial indexing."""
    cached_path = os.path.join(TEMP_FGB_CACHE_DIR, f"{layer.uuid}.fgb")
    if os.path.exists(cached_path):
        return cached_path

    storage = layer.storage_params or {}
    bucket = storage.get("bucket")
    object_name = storage.get("object_name")

    if bucket and object_name:
        if not minio_manager:
            raise HTTPException(status_code=500, detail="MinIO manager unavailable.")
        minio_manager.download_file(
            bucket_name=bucket,
            object_name=object_name,
            destination_path=cached_path,
        )
        return cached_path

    rel_path = storage.get("file_path", "")
    local_path = os.path.join(MEDIA_BASE, rel_path.lstrip("/\\"))
    if not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="Local FlatGeobuf file not found.")
    return local_path



@dch_temp_router.post(
    "/upload/",
    summary="Upload and ingest temporary layer into TempLayerInfo",
    status_code=status.HTTP_201_CREATED,
)
async def upload_temp_layer(
    request: Request,
    title: str = Form(..., description="Temporary Layer Name"),
    symbology: Optional[str] = Form(None, description="Serialized IFeatureStyle JSON"),
    days_to_keep: int = Form(14, description="TTL in days before auto-purge"),
    files: Optional[List[UploadFile]] = File(None, description="Uploaded shapefile files"),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    uploaded_files: List[UploadFile] = files or []
    if not uploaded_files:
        form = await request.form()
        for _, val in form.multi_items():
            if isinstance(val, UploadFile) and val.filename:
                uploaded_files.append(val)

    if not uploaded_files:
        raise HTTPException(status_code=400, detail="No files received in upload request.")

    # 1. Parse symbology
    parsed_symbology = None
    if symbology:
        try:
            parsed_symbology = json.loads(symbology) if isinstance(symbology, str) else symbology
        except Exception as e:
            da_logger.warning("Could not parse temp layer symbology: %s", e)

    # 2. Stage shapefile parts into temp directory
    with tempfile.TemporaryDirectory(prefix="temp_shp_") as stage_dir:
        shp_path = None
        saved_names = []

        for uf in uploaded_files:
            file_name = os.path.basename(uf.filename)
            dest = os.path.join(stage_dir, file_name)
            content = await uf.read()
            with open(dest, "wb") as f:
                f.write(content)
            saved_names.append(file_name)
            if file_name.lower().endswith(".shp"):
                shp_path = dest

        if not shp_path:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required .shp file. Received files: {saved_names}",
            )

        try:
            gdf = gpd.read_file(shp_path)
        except Exception as err:
            da_logger.exception("Failed to read shapefile at %s: %s", shp_path, err)
            raise HTTPException(status_code=400, detail=f"Corrupted or invalid Shapefile: {str(err)}")

        if gdf.empty:
            raise HTTPException(status_code=400, detail="Uploaded shapefile contains no features.")

        # Ensure valid CRS
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)
        srid = gdf.crs.to_epsg() or 4326
        if srid != 4326:
            gdf = gdf.to_crs(epsg=4326)

        gdf.columns = gdf.columns.str.lower()
        gdf.rename_geometry("geom", inplace=True)
        columns_list = [c for c in gdf.columns if c != "geom"]
        geom_types = list(gdf.geom_type.unique())
        primary_geom = geom_types[0] if geom_types else "Polygon"

        bounds_4326 = [float(v) for v in gdf.total_bounds]
        try:
            extent_3857 = [float(v) for v in gdf.to_crs(epsg=3857).total_bounds]
        except Exception:
            extent_3857 = bounds_4326

        uuid_str = DCHUtils.get_uuid1()
        safe_title = DCHUtils.short_table_name(title, max_len=40)
        fgb_filename = f"{safe_title}_{uuid_str}.fgb"
        local_fgb = os.path.join(stage_dir, fgb_filename)

        # 3. Create FlatGeobuf with Packed Hilbert R-Tree index
        gdf.to_file(local_fgb, driver="FlatGeobuf", spatial_index=True)

        storage_dict = {
            "geometry_col": "geom",
            "columns": columns_list,
            "feature_count": len(gdf),
        }

        # 4. Storage Persistence
        if minio_manager is not None:
            object_key = f"temp_layers/user_{current_user.id}/{fgb_filename}"
            uploaded = minio_manager.upload_file(
                bucket_name=MINIO_BUCKET_NAME,
                object_name=object_key,
                file_path=local_fgb,
                content_type="application/octet-stream",
            )
            if not uploaded:
                raise HTTPException(status_code=500, detail="Failed to upload FlatGeobuf to MinIO.")
            storage_dict.update({"bucket": MINIO_BUCKET_NAME, "object_name": object_key})
        else:
            dest_dir = os.path.join(MEDIA_BASE, "temp_layers", f"user_{current_user.id}")
            os.makedirs(dest_dir, exist_ok=True)
            final_path = os.path.join(dest_dir, fgb_filename)
            import shutil
            shutil.copy2(local_fgb, final_path)
            storage_dict.update({"file_path": os.path.relpath(final_path, MEDIA_BASE)})

        # 5. Insert directly into TempLayerInfo
        temp_layer = TempLayerInfo(
            uuid=uuid_str,
            title=title.strip(),
            user_id=current_user.id,
            geom_type=primary_geom,
            srid=4326,
            extent=bounds_4326,
            extent_3857=extent_3857,
            feature_count=len(gdf),
            format="FlatGeobuf",
            storage_params=storage_dict,
            layer_styling=parsed_symbology,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=days_to_keep),
        )
        session.add(temp_layer)
        session.commit()

        return {
            "success": True,
            "uuid": temp_layer.uuid,
            "title": temp_layer.title,
            "geomType": temp_layer.geom_type,
            "featureCount": temp_layer.feature_count,
            "srid": temp_layer.srid,
        }


@dch_temp_router.get("/list/", summary="List all active TempLayerInfo for user")
def list_temp_layers(
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    query = (
        select(TempLayerInfo)
        .where(TempLayerInfo.user_id == current_user.id)
        .order_by(TempLayerInfo.created_at.desc())
    )
    layers = session.execute(query).scalars().all()

    return {
        "payload": [
            {
                "uuid": lyr.uuid,
                "title": lyr.title,
                "geomType": lyr.geom_type,
                "createdAt": lyr.created_at.strftime("%Y-%m-%d %H:%M") if lyr.created_at else "",
                "featureCount": lyr.feature_count,
                "style": lyr.layer_styling or {},
            }
            for lyr in layers
            if not lyr.is_expired
        ]
    }


@dch_temp_router.get("/geojson/{uuid}/", summary="Get TempLayerInfo features as GeoJSON")
def get_temp_layer_geojson(
    uuid: str,
    session: Session = Depends(get_sync_session),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    layer = session.execute(
        select(TempLayerInfo).where(
            TempLayerInfo.uuid == uuid,
            TempLayerInfo.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if not layer:
        raise HTTPException(status_code=404, detail="Temporary layer not found.")

    local_fgb = _resolve_cached_fgb(layer, minio_manager)
    gdf = gpd.read_file(local_fgb)

    if gdf.empty:
        return Response(status_code=204)

    # Standardize to WGS84 for GeoJSON display
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    geojson_obj = GeoJSON.from_gdf(gdf)

    # Match the standard DCH endpoint response envelope:
    return {
        "payload": DCHUtils.gzip_data(geojson_obj.model_dump_json(by_alias=True)),
        "meta": {"format": "application/gzip", "count": len(gdf), "uuid": uuid},
    }


@dch_temp_router.get(
    "/mvt/{uuid}/{z}/{x}/{y}.pbf",
    summary="Dynamic Vector Tile from TempLayerInfo FlatGeobuf",
    response_class=Response,
)
def get_temp_layer_mvt(
    uuid: str,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_sync_session),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    layer = session.execute(
        select(TempLayerInfo).where(TempLayerInfo.uuid == uuid)
    ).scalar_one_or_none()

    if not layer:
        return Response(content=b"", media_type="application/x-protobuf")

    try:
        local_fgb = _resolve_cached_fgb(layer, minio_manager)
        tile_bounds = mercantile.bounds(x, y, z)
        bbox = (tile_bounds.west, tile_bounds.south, tile_bounds.east, tile_bounds.north)

        # Fast indexed read
        gdf = gpd.read_file(local_fgb, bbox=bbox)
        if gdf.empty:
            return Response(content=b"", media_type="application/x-protobuf")

        env_3857 = MVTUtils.xyz_to_extent_3857(x, y, z)
        mvt_bytes = MVTUtils.gdf_to_mvt(
            gdf=gdf,
            tile_extent_3857=(
                env_3857["minx"].iloc[0],
                env_3857["miny"].iloc[0],
                env_3857["maxx"].iloc[0],
                env_3857["maxy"].iloc[0],
            ),
            name=layer.title,
        )
        return Response(content=mvt_bytes, media_type="application/x-protobuf")
    except Exception as e:
        da_logger.exception("Failed to generate MVT for TempLayerInfo %s: %s", uuid, e)
        return Response(content=b"", media_type="application/x-protobuf")


@dch_temp_router.delete("/delete/{uuid}/", summary="Delete TempLayerInfo and file")
def delete_temp_layer(
    uuid: str,
    session: Session = Depends(get_sync_session),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    layer = session.execute(
        select(TempLayerInfo).where(TempLayerInfo.uuid == uuid)
    ).scalar_one_or_none()

    if not layer:
        raise HTTPException(status_code=404, detail="Layer not found.")

    if not current_user.is_superuser and layer.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Permission denied.")

    storage = layer.storage_params or {}
    bucket = storage.get("bucket")
    object_name = storage.get("object_name")

    try:
        if bucket and object_name and minio_manager:
            minio_manager.delete_object(bucket_name=bucket, object_name=object_name)
        elif "file_path" in storage:
            local_path = os.path.join(MEDIA_BASE, storage["file_path"].lstrip("/\\"))
            if os.path.exists(local_path):
                os.remove(local_path)
    except Exception as e:
        da_logger.warning("Error removing physical file for TempLayerInfo %s: %s", uuid, e)

    session.delete(layer)
    session.commit()
    return {"success": True, "message": f"Temporary layer '{uuid}' deleted."}


@dch_temp_router.patch(
    "/style/{uuid}/",
    summary="Update symbology for a temporary layer",
)
def update_temp_layer_style(
    uuid: str,
    payload: dict = Body(..., description="Expected {'symbology': {...}}"),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    layer = session.execute(
        select(TempLayerInfo).where(
            TempLayerInfo.uuid == uuid,
            TempLayerInfo.user_id == current_user.id,
        )
    ).scalar_one_or_none()

    if not layer:
        raise HTTPException(status_code=404, detail="Temporary layer not found.")

    new_style = payload.get("symbology")
    layer.layer_styling = new_style
    session.commit()

    return {"success": True, "message": "Symbology updated successfully.", "uuid": uuid}