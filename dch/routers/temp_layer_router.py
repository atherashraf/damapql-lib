from __future__ import annotations

import base64
import gzip
import io
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import mercantile
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import TempLayerInfo
from app.damapql.dch.services.ingestion.staging_storage import MinioStagingStorage, LocalStagingStorage, StagingStorage
from app.damapql.dch.utils.mvt_utils import MVTUtils
from app.damapql.settings import IS_LOCAL, MEDIA_BASE, MINIO_BUCKET_NAME
from app.damapql.utils.adapters.deps import get_minio_manager, get_sync_session
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.utils.singletons import da_logger

dch_temp_router = APIRouter(prefix="/temp_layers", tags=["DCH Temporary Layers"])

TEMP_STORAGE_SUBDIR = "temp_layers"
TEMP_CACHE_DIR = os.path.join(tempfile.gettempdir(), "dch_temp_layer_cache")
os.makedirs(TEMP_CACHE_DIR, exist_ok=True)


# =============================================================================
# 1. STAGING & PERSISTENT STORAGE HELPERS
# =============================================================================

def _get_staging_storage(minio_manager: Optional[MinioManager] = None) -> StagingStorage:
    if minio_manager is not None and not IS_LOCAL:
        return MinioStagingStorage(
            minio_manager=minio_manager,
            bucket_name=MINIO_BUCKET_NAME,
            prefix=f"{TEMP_STORAGE_SUBDIR}/_staging",
            delete_remote_on_cleanup=True,
        )
    return LocalStagingStorage()


def _get_local_temp_dir(layer_uuid: str) -> Path:
    base = Path(MEDIA_BASE) / TEMP_STORAGE_SUBDIR / layer_uuid
    base.mkdir(parents=True, exist_ok=True)
    return base


def _store_artifact(
    layer_uuid: str,
    filename: str,
    local_file_path: str,
    minio_manager: Optional[MinioManager] = None,
) -> Dict[str, Any]:
    """
    Promotes a processed artifact (FGB or COG) from staging into durable storage
    (MinIO or MEDIA_BASE) and primes the local tile/MVT cache.
    """
    cached_path = os.path.join(TEMP_CACHE_DIR, f"{layer_uuid}_{filename}")
    shutil.copy2(local_file_path, cached_path)

    if minio_manager is not None and not IS_LOCAL:
        object_key = f"{TEMP_STORAGE_SUBDIR}/{layer_uuid}/{filename}"
        uploaded = minio_manager.upload_file(
            bucket_name=MINIO_BUCKET_NAME,
            object_name=object_key,
            file_path=local_file_path,
        )
        if not uploaded:
            raise RuntimeError(f"Failed to upload artifact to MinIO: s3://{MINIO_BUCKET_NAME}/{object_key}")

        return {
            "storage_backend": "minio",
            "bucket": MINIO_BUCKET_NAME,
            "object_key": object_key,
            "object_name": object_key,
            "filename": filename,
        }

    dest_dir = _get_local_temp_dir(layer_uuid)
    dest_path = dest_dir / filename
    shutil.copy2(local_file_path, dest_path)
    rel_path = f"{TEMP_STORAGE_SUBDIR}/{layer_uuid}/{filename}"
    return {
        "storage_backend": "local",
        "local_path": str(dest_path),
        "file_path": rel_path,
        "filename": filename,
    }


def _resolve_cached_artifact(
    layer: TempLayerInfo,
    minio_manager: Optional[MinioManager],
    default_ext: str = ".fgb",
) -> str:
    """
    Resolves a persistent local cached path for FGB or COG files so repeated
    tile/MVT requests do not re-download from MinIO on every tile.
    """
    storage = layer.storage_params or {}
    filename = storage.get("filename") or f"{layer.uuid}{default_ext}"
    cached_path = os.path.join(TEMP_CACHE_DIR, f"{layer.uuid}_{filename}")

    if os.path.exists(cached_path):
        return cached_path

    backend = storage.get("storage_backend")
    bucket = storage.get("bucket")
    object_key = storage.get("object_key") or storage.get("object_name")

    if (backend == "minio" or (bucket and object_key)) and object_key:
        if not minio_manager:
            raise HTTPException(status_code=500, detail="MinIO manager unavailable.")
        minio_manager.download_file(
            bucket_name=bucket or MINIO_BUCKET_NAME,
            object_name=object_key,
            destination_path=cached_path,  # Changed from file_path=cached_path
        )
        return cached_path

    local_path = storage.get("local_path")
    if not local_path and storage.get("file_path"):
        local_path = os.path.join(MEDIA_BASE, storage["file_path"].lstrip("/\\"))

    if not local_path or not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="Temporary layer artifact not found on storage.")

    shutil.copy2(local_path, cached_path)
    return cached_path


def _delete_artifact(
    layer_uuid: str,
    storage_params: Dict[str, Any],
    minio_manager: Optional[MinioManager] = None,
) -> None:
    for entry in Path(TEMP_CACHE_DIR).glob(f"{layer_uuid}_*"):
        entry.unlink(missing_ok=True)

    backend = storage_params.get("storage_backend", "local")
    object_key = storage_params.get("object_key") or storage_params.get("object_name")

    if backend == "minio" and minio_manager is not None and object_key:
        try:
            minio_manager.delete_object(
                storage_params.get("bucket", MINIO_BUCKET_NAME),
                object_key,
            )
        except Exception as exc:
            da_logger.warning(f"[TempLayer] Failed to delete MinIO object {object_key}: {exc}")
    else:
        local_dir = Path(MEDIA_BASE) / TEMP_STORAGE_SUBDIR / layer_uuid
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)


# =============================================================================
# 2. COGRaster STYLING & TILE BRIDGE
# =============================================================================

def _build_cog_color_map(
    cog: COGRaster,
    style_cfg: Dict[str, Any],
    band_stats: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Any]:
    """
    Translates frontend RasterLayerStylizer config (`pseudocolor` / `stretch`)
    into COGRaster's native `create_color_map()` interval list, falling back to
    complete-raster `global_minmax` or `RioRaster.get_band_summaries()` bounds.
    """
    mode = style_cfg.get("type", "pseudocolor")
    if mode == "rgb":
        return None

    band_idx = int(style_cfg.get("band", 1))
    vmin = style_cfg.get("min")
    vmax = style_cfg.get("max")

    # Resolve full-raster min/max if not explicitly overridden
    if vmin is None or vmax is None:
        if band_stats and 1 <= band_idx <= len(band_stats):
            stats_entry = band_stats[band_idx - 1]
            if vmin is None:
                vmin = stats_entry.get("min")
            if vmax is None:
                vmax = stats_entry.get("max")
        if vmin is None or vmax is None:
            g_min, g_max = cog.global_minmax
            vmin = g_min if vmin is None else vmin
            vmax = g_max if vmax is None else vmax

    vmin = float(vmin)
    vmax = float(vmax)
    if vmax <= vmin:
        vmax = vmin + 1e-6

    ramp_colors: List[str] = style_cfg.get("colorRamp") or []
    if mode == "stretch" or len(ramp_colors) < 2:
        ramp_colors = ["#000000", "#ffffff"]

    class_method = style_cfg.get("classificationMethod", "Continuous")
    is_discrete = class_method != "Continuous"

    # Build style_info dict compatible with COGRaster.create_color_map(style_info)
    try:
        if is_discrete:
            n_classes = len(ramp_colors)
            step = (vmax - vmin) / n_classes
            classes = []
            for idx, hex_color in enumerate(ramp_colors):
                c_min = vmin + idx * step
                c_max = float("inf") if idx == n_classes - 1 else vmin + (idx + 1) * step
                classes.append({
                    "min": c_min,
                    "max": c_max,
                    "color": hex_color,
                    "label": f"{c_min:.2f} - {c_max:.2f}",
                })
            return COGRaster.create_color_map({
                "type": "discrete",
                "classes": classes,
            })

        # Continuous ramp: build evenly spaced color stops across [vmin, vmax]

        return COGRaster.create_color_map({
            "type": "continuous",
            "min": vmin,
            "max": vmax,
            "palette": ramp_colors,
        })


    except Exception as exc:
        da_logger.debug(f"[TempRasterTile] COGRaster.create_color_map fallback: {exc}")
        return None


def _render_rgb_composite_tile(
    cog: COGRaster,
    x: int,
    y: int,
    z: int,
    style_cfg: Dict[str, Any],
) -> bytes:
    """
    Renders a custom 3-band RGB composite tile using COGRaster's environment
    and bounds when custom band indices or min/max stretch values are specified.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    rgb_bands = style_cfg.get("rgbBands") or {"red": 1, "green": 2, "blue": 3}
    vmin = style_cfg.get("min")
    vmax = style_cfg.get("max")

    # If default bands [1, 2, 3] and no custom min/max stretch, delegate directly to COGRaster
    if (
        rgb_bands.get("red", 1) == 1
        and rgb_bands.get("green", 2) == 2
        and rgb_bands.get("blue", 3) == 3
        and vmin is None
        and vmax is None
        and cog.count >= 3
    ):
        return cog.read_tile_as_png(x=x, y=y, z=z).getvalue()

    try:
        tile_bounds = mercantile.bounds(x, y, z)
        with cog._env:
            with rasterio.open(cog.file_path) as src:
                b_indices = [
                    min(max(int(rgb_bands.get("red", 1)), 1), src.count),
                    min(max(int(rgb_bands.get("green", 2)), 1), src.count),
                    min(max(int(rgb_bands.get("blue", 3)), 1), src.count),
                ]
                dst_bounds = transform_bounds("EPSG:4326", src.crs, *tile_bounds, densify_pts=21)
                window = from_bounds(*dst_bounds, transform=src.transform)

                data = src.read(
                    b_indices,
                    window=window,
                    out_shape=(3, 256, 256),
                    resampling=Resampling.bilinear,
                    boundless=True,
                    fill_value=src.nodata if src.nodata is not None else 0,
                ).astype(np.float32)
                mask = src.dataset_mask(window=window, out_shape=(256, 256), boundless=True)

        lo = float(vmin) if vmin is not None else cog.global_minmax[0]
        hi = float(vmax) if vmax is not None else cog.global_minmax[1]
        span = max(hi - lo, 1e-6)

        norm = np.clip((data - lo) / span * 255.0, 0, 255).astype(np.uint8)
        rgb = norm.transpose(1, 2, 0)
        rgba = np.concatenate([rgb, mask[:, :, np.newaxis]], axis=2).astype(np.uint8)
        return COGRaster.create_image(rgba).getvalue()
    except Exception as exc:
        da_logger.error(f"[TempRasterRGBTile] Error at z={z}, x={x}, y={y}: {exc}")
        return COGRaster.create_empty_image(256, 256).getvalue()


# =============================================================================
# 3. VECTOR & RASTER UPLOAD ENDPOINTS (Using StagingStorage + COGRaster/RioRaster)
# =============================================================================

@dch_temp_router.post("/upload/", status_code=201)
def upload_temp_vector_layer(
    title: str = Form(...),
    days_to_keep: int = Form(14),
    symbology: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    """Ingests uploaded Shapefile bundle via StagingStorage into FlatGeobuf (.fgb)."""
    valid_files = [f for f in files if f.filename]
    if not valid_files:
        raise HTTPException(status_code=400, detail="No shapefile files uploaded")

    layer_uuid = str(uuid.uuid4())
    parsed_style = json.loads(symbology) if symbology else None
    staging = _get_staging_storage(minio_manager)

    staged_inputs = []
    fgb_scratch = None

    try:
        shp_path: Optional[str] = None
        base_staged = staging.stage_upload(valid_files[0], os.path.basename(valid_files[0].filename))
        staged_inputs.append(base_staged)
        bundle_dir = base_staged.staging_dir or os.path.dirname(base_staged.local_path)

        if base_staged.local_path.lower().endswith(".shp"):
            shp_path = base_staged.local_path

        for up in valid_files[1:]:
            safe_name = os.path.basename(up.filename)
            dest = os.path.join(bundle_dir, safe_name)
            up.file.seek(0)
            with open(dest, "wb") as dst:
                while chunk := up.file.read(1024 * 1024):
                    dst.write(chunk)
            if safe_name.lower().endswith(".shp"):
                shp_path = dest

        if not shp_path:
            raise HTTPException(status_code=400, detail="Missing .shp file in bundle")

        gdf = gpd.read_file(shp_path)
        if gdf.empty:
            raise HTTPException(status_code=400, detail="Shapefile contains 0 features")

        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)
        else:
            gdf = gdf.to_crs("EPSG:4326")

        raw_geom = str(gdf.geometry.geom_type.dropna().iloc[0])
        geom_type = (
            "Point" if "Point" in raw_geom
            else "LineString" if "Line" in raw_geom
            else "Polygon"
        )

        bounds_4326 = [float(x) for x in gdf.total_bounds]
        gdf_3857 = gdf.to_crs("EPSG:3857")
        bounds_3857 = [float(x) for x in gdf_3857.total_bounds]

        fgb_filename = f"{layer_uuid}.fgb"
        fgb_scratch = staging.create_scratch_file(fgb_filename)
        gdf.to_file(fgb_scratch.local_path, driver="FlatGeobuf")

        storage_meta = _store_artifact(layer_uuid, fgb_filename, fgb_scratch.local_path, minio_manager)
        storage_meta["data_model"] = "V"

        expires_at = datetime.utcnow() + timedelta(days=days_to_keep)
        record = TempLayerInfo(
            uuid=layer_uuid,
            title=title.strip(),
            user_id=current_user.id,
            geom_type=geom_type,
            srid=4326,
            extent=bounds_4326,
            extent_3857=bounds_3857,
            feature_count=int(len(gdf)),
            format="FlatGeobuf",
            storage_params=storage_meta,
            layer_styling=parsed_style,
            expires_at=expires_at,
        )
        session.add(record)
        session.commit()

        return {
            "uuid": layer_uuid,
            "title": record.title,
            "dataModel": "V",
            "geomType": geom_type,
            "featureCount": record.feature_count,
        }
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        da_logger.error(f"[TempVectorUpload] Failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Vector ingestion failed: {str(exc)}")
    finally:
        for item in staged_inputs:
            staging.cleanup(item)
        if fgb_scratch is not None:
            staging.cleanup(fgb_scratch)


@dch_temp_router.post("/upload_raster/", status_code=201)
def upload_temp_raster_layer(
    title: str = Form(...),
    days_to_keep: int = Form(14),
    convert_to_cog: bool = Form(True),
    symbology: Optional[str] = Form(None),
    rasterFile: UploadFile = File(...),
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    """
    Stages uploaded GeoTIFF/Raster via StagingStorage, validates/converts to COG
    using COGRaster.create_cog(), extracts complete raster band stats & envelopes
    via RioRaster & COGRaster, and persists TempLayerInfo.
    """
    if not rasterFile.filename:
        raise HTTPException(status_code=400, detail="No raster file uploaded")

    layer_uuid = str(uuid.uuid4())
    parsed_style = json.loads(symbology) if symbology else {
        "type": "pseudocolor",
        "opacity": 1,
        "band": 1,
    }

    staging = _get_staging_storage(minio_manager)
    safe_name = os.path.basename(rasterFile.filename)
    cog_filename = f"{layer_uuid}_cog.tif"
    cog_scratch = None

    try:
        with staging.stage(rasterFile, safe_name) as staged_raw:
            if convert_to_cog:
                if COGRaster.is_cog(staged_raw.local_path):
                    final_raster_path = staged_raw.local_path
                else:
                    cog_scratch = staging.create_scratch_file(cog_filename)
                    final_raster_path = COGRaster.create_cog(
                        src=staged_raw.local_path,
                        des_path=cog_scratch.local_path,
                    )
            else:
                final_raster_path = staged_raw.local_path

            # Reuse COGRaster & RioRaster for complete raster bounds and band summaries
            cog = COGRaster.open_from_local(final_raster_path)
            extent_4326 = [float(x) for x in cog.get_envelop(srid=4326).total_bounds]
            extent_3857 = [float(x) for x in cog.get_envelop(srid=3857).total_bounds]
            band_count = cog.get_spectral_resolution()
            srid = cog.get_raster_srid() or 4326

            rio_raster = RioRaster(final_raster_path)
            band_stats: List[Dict[str, Any]] = []
            try:
                summaries_df = rio_raster.get_band_summaries()
                for idx, (_, row) in enumerate(summaries_df.iterrows(), start=1):
                    band_stats.append({
                        "band": idx,
                        "min": float(row.get("min", cog.global_minmax[0])),
                        "max": float(row.get("max", cog.global_minmax[1])),
                        "mean": float(row.get("mean", 0.0)),
                        "std": float(row.get("std", 0.0)),
                    })
            except Exception as stats_exc:
                da_logger.warning(f"[TempRasterUpload] Fallback to global_minmax: {stats_exc}")
                g_min, g_max = cog.global_minmax
                for idx in range(1, band_count + 1):
                    band_stats.append({"band": idx, "min": float(g_min), "max": float(g_max), "mean": 0.0})
            finally:
                rio_raster.close()

            # Auto-populate full-raster min/max in initial style if left at default bounds
            if band_stats and parsed_style.get("min") in (None, 0) and parsed_style.get("max") in (None, 255):
                target_b = min(max(int(parsed_style.get("band", 1)), 1), len(band_stats)) - 1
                parsed_style["min"] = round(band_stats[target_b]["min"], 4)
                parsed_style["max"] = round(band_stats[target_b]["max"], 4)

            storage_meta = _store_artifact(
                layer_uuid,
                cog_filename,
                final_raster_path,
                minio_manager,
            )
            storage_meta.update({
                "data_model": "R",
                "band_count": band_count,
                "stats": band_stats,
                "global_minmax": list(cog.global_minmax),
            })

            expires_at = datetime.utcnow() + timedelta(days=days_to_keep)
            record = TempLayerInfo(
                uuid=layer_uuid,
                title=title.strip(),
                user_id=current_user.id,
                geom_type="Raster",
                srid=srid,
                extent=extent_4326,
                extent_3857=extent_3857,
                feature_count=0,
                format="COG",
                storage_params=storage_meta,
                layer_styling=parsed_style,
                expires_at=expires_at,
            )
            session.add(record)
            session.commit()

            tile_url = f"api/temp_layers/tiles/{layer_uuid}/{{z}}/{{x}}/{{y}}.png"
            return {
                "uuid": layer_uuid,
                "title": record.title,
                "dataModel": "R",
                "tileUrl": tile_url,
                "extent_3857": extent_3857,
                "band_count": band_count,
                "stats": band_stats,
                "style": parsed_style,
            }
    except HTTPException:
        raise
    except Exception as exc:
        session.rollback()
        da_logger.error(f"[TempRasterUpload] Failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Raster ingestion failed: {str(exc)}")
    finally:
        if cog_scratch is not None:
            staging.cleanup(cog_scratch)


# =============================================================================
# 4. LIST, GEOJSON, MVT, RASTER TILES, STYLE UPDATE & DELETE
# =============================================================================

@dch_temp_router.get("/list/")
def list_temp_layers(
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """Lists all non-expired temporary vector and raster layers for the current user."""
    records = (
        session.query(TempLayerInfo)
        .filter(TempLayerInfo.user_id == current_user.id)
        .order_by(TempLayerInfo.created_at.desc())
        .all()
    )

    payload = []
    for rec in records:
        if rec.is_expired:
            continue

        params = rec.storage_params or {}
        is_raster = (
            params.get("data_model") == "R"
            or rec.format == "COG"
            or rec.geom_type == "Raster"
        )
        data_model = "R" if is_raster else "V"

        item: Dict[str, Any] = {
            "uuid": rec.uuid,
            "title": rec.title,
            "dataModel": data_model,
            "createdAt": rec.created_at.strftime("%Y-%m-%d %H:%M") if rec.created_at else "",
            "style": rec.layer_styling,
        }

        if is_raster:
            item["tileUrl"] = f"api/temp_layers/tiles/{rec.uuid}/{{z}}/{{x}}/{{y}}.png"
            item["extent_3857"] = rec.extent_3857
            item["band_count"] = params.get("band_count")
            item["stats"] = params.get("stats")
        else:
            item["geomType"] = rec.geom_type or "Polygon"
            item["featureCount"] = rec.feature_count

        payload.append(item)

    return {"payload": payload}


@dch_temp_router.get("/geojson/{uuid}/")
def get_temp_layer_geojson(
    uuid: str,
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    """Streams compressed GeoJSON for a temporary FlatGeobuf vector layer."""
    record = session.query(TempLayerInfo).filter(TempLayerInfo.uuid == uuid).first()
    if not record:
        raise HTTPException(status_code=404, detail="Temporary layer not found")

    fgb_path = _resolve_cached_artifact(record, minio_manager, default_ext=".fgb")
    gdf = gpd.read_file(fgb_path)
    geojson_str = gdf.to_json()
    compressed = gzip.compress(geojson_str.encode("utf-8"))
    return {"payload": base64.b64encode(compressed).decode("ascii")}


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
        local_fgb = _resolve_cached_artifact(layer, minio_manager, default_ext=".fgb")
        tile_bounds = mercantile.bounds(x, y, z)
        bbox = (tile_bounds.west, tile_bounds.south, tile_bounds.east, tile_bounds.north)

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


@dch_temp_router.get("/tiles/{uuid}/{z}/{x}/{y}.png")
def get_temp_raster_tile(
    uuid: str,
    z: int,
    x: int,
    y: int,
    style: Optional[str] = Query(None, description="Optional JSON style override for live preview"),
    session: Session = Depends(get_sync_session),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    """Renders an XYZ PNG tile from a temporary COG using COGRaster.read_tile_as_png."""
    record = session.query(TempLayerInfo).filter(TempLayerInfo.uuid == uuid).first()
    if not record:
        raise HTTPException(status_code=404, detail="Temporary raster layer not found")

    active_style: Dict[str, Any] = dict(record.layer_styling or {})
    if style:
        try:
            active_style.update(json.loads(style))
        except Exception:
            pass

    raster_path = _resolve_cached_artifact(record, minio_manager, default_ext="_cog.tif")
    cog = COGRaster.open_from_local(raster_path)
    params = record.storage_params or {}

    if active_style.get("type") == "rgb":
        png_bytes = _render_rgb_composite_tile(cog, x=x, y=y, z=z, style_cfg=active_style)
    else:
        color_map = _build_cog_color_map(cog, active_style, band_stats=params.get("stats"))
        png_buf = cog.read_tile_as_png(x=x, y=y, z=z, color_map=color_map)
        png_bytes = png_buf.getvalue()

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@dch_temp_router.patch("/style/{uuid}/")
def update_temp_layer_style(
    uuid: str,
    body: Dict[str, Any],
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """Updates vector or raster symbology JSON in TempLayerInfo.layer_styling."""
    record = (
        session.query(TempLayerInfo)
        .filter(TempLayerInfo.uuid == uuid, TempLayerInfo.user_id == current_user.id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Temporary layer not found")

    new_style = body.get("symbology")
    if new_style is None:
        raise HTTPException(status_code=400, detail="Missing 'symbology' payload")

    record.layer_styling = new_style
    flag_modified(record, "layer_styling")
    session.commit()
    return {"success": True, "uuid": uuid, "style": record.layer_styling}


@dch_temp_router.delete("/delete/{uuid}/")
def delete_temp_layer(
    uuid: str,
    session: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: Optional[MinioManager] = Depends(get_minio_manager),
):
    """Deletes temporary layer metadata and removes underlying FGB/COG artifact."""
    record = (
        session.query(TempLayerInfo)
        .filter(TempLayerInfo.uuid == uuid, TempLayerInfo.user_id == current_user.id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Temporary layer not found")

    _delete_artifact(uuid, record.storage_params or {}, minio_manager)
    session.delete(record)
    session.commit()
    return {"success": True, "uuid": uuid}