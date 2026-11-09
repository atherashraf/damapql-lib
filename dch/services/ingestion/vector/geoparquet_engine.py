# app/damapql/dch/services/ingestion/vector/geoparquet_engine.py
from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

import geopandas as gpd
import mapbox_vector_tile
import mercantile
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer
from shapely.geometry import box
from sqlalchemy.orm import Session

from app.damapql.dch.models import AccessibilityType, LayerCategory
from app.damapql.dch.services.ingestion.vector.base import (
    BaseVectorStorageEngine,
    EnginePersistenceResult,
    VectorStorageEngineType,
)
from app.damapql.settings import DCH_APP_NAME, MEDIA_BASE
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


def build_pmtiles_from_gdf(
    gdf: gpd.GeoDataFrame,
    layer_name: str,
    output_path: str,
    min_zoom: int = 10,
    max_zoom: int = 18,
) -> None:
    """Pure-Python PMTiles v3 generator using mercantile, MVT, and pmtiles."""
    if gdf.empty:
        return

    gdf_4326 = gdf.to_crs(epsg=4326) if gdf.crs and gdf.crs.to_epsg() != 4326 else gdf
    minx, miny, maxx, maxy = gdf_4326.total_bounds

    export_gdf = gdf_4326.copy()
    for col in export_gdf.columns:
        if col != "geometry":
            if not export_gdf[col].dtype.name.startswith(("int", "float", "bool")):
                export_gdf[col] = export_gdf[col].astype(str)

    sindex = export_gdf.sindex

    with open(output_path, "wb") as f:
        writer = Writer(f)

        for z in range(min_zoom, max_zoom + 1):
            tiles = list(mercantile.tiles(minx, miny, maxx, maxy, zooms=z))

            for t in tiles:
                tile_bbox = mercantile.bounds(t)
                matches_idx = list(
                    sindex.intersection((tile_bbox.west, tile_bbox.south, tile_bbox.east, tile_bbox.north))
                )
                if not matches_idx:
                    continue

                subset = export_gdf.iloc[matches_idx]
                tile_geom = box(tile_bbox.west, tile_bbox.south, tile_bbox.east, tile_bbox.north)
                intersecting = subset[subset.intersects(tile_geom)]
                if intersecting.empty:
                    continue

                features: List[Dict[str, Any]] = []
                for _, row in intersecting.iterrows():
                    props = {k: v for k, v in row.items() if k != "geometry"}
                    features.append({
                        "geometry": row.geometry.__geo_interface__,
                        "properties": props,
                    })

                raw_mvt = mapbox_vector_tile.encode(
                    layers=[{"name": layer_name, "features": features}],
                    quantize_bounds=[tile_bbox.west, tile_bbox.south, tile_bbox.east, tile_bbox.north],
                    extents=4096,
                )

                if raw_mvt:
                    compressed_mvt = gzip.compress(raw_mvt)
                    tile_id = zxy_to_tileid(t.z, t.x, t.y)
                    writer.write_tile(tile_id, compressed_mvt)

        header = {
            "tile_type": TileType.MVT,
            "tile_compression": Compression.GZIP,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "min_lon": float(minx),
            "min_lat": float(miny),
            "max_lon": float(maxx),
            "max_lat": float(maxy),
            "center_zoom": int((min_zoom + max_zoom) // 2),
            "center_lon": float((minx + maxx) / 2),
            "center_lat": float((miny + maxy) / 2),
        }

        metadata = {
            "vector_layers": [
                {
                    "id": layer_name,
                    "fields": {
                        col: str(export_gdf[col].dtype)
                        for col in export_gdf.columns
                        if col != "geometry"
                    },
                    "minzoom": min_zoom,
                    "maxzoom": max_zoom,
                }
            ],
            "tilestats": {
                "layerCount": 1,
                "layers": [
                    {
                        "layer": layer_name,
                        "count": len(export_gdf),
                        "geometry": export_gdf.geometry.geom_type.iloc[0] if len(export_gdf) > 0 else "Unknown",
                    }
                ],
            },
        }

        writer.finalize(header, metadata)


class GeoParquetVectorEngine(BaseVectorStorageEngine):
    engine_type = VectorStorageEngineType.GEOPARQUET

    def persist(
        self,
        *,
        session: Session,
        gdf: gpd.GeoDataFrame,
        safe_title: str,
        uuid_str: str,
        category: LayerCategory,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str,
        target_srid: int = 4326,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> EnginePersistenceResult:
        sub_cat = category.sub or "general"
        parquet_filename = f"{safe_title}_{uuid_str}.parquet"
        pmtiles_filename = f"{safe_title}_{uuid_str}.pmtiles"
        compression = (extra_params or {}).get("compression", "snappy")

        created_minio_object = None
        created_local_file = None
        created_pmtiles_minio_object = None
        created_pmtiles_local_file = None

        with tempfile.TemporaryDirectory(prefix="dch_parquet_") as tmp_dir:
            local_parquet_path = os.path.join(tmp_dir, parquet_filename)
            local_pmtiles_path = os.path.join(tmp_dir, pmtiles_filename)

            # 1. Write GeoParquet for analytics
            gdf.to_parquet(local_parquet_path, index=False, compression=compression, schema_version="1.0.0")

            # 2. Build companion PMTiles for map visualization
            build_pmtiles_from_gdf(gdf, safe_title, local_pmtiles_path)

            column_names = [col for col in gdf.columns if col != gdf.geometry.name]

            # 3A. Persist to MinIO
            if minio_manager is not None:
                parquet_object_key = f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/parquet/{parquet_filename}"
                pmtiles_object_key = f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/tiles/{pmtiles_filename}"

                # Upload Parquet
                if not minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=parquet_object_key,
                    file_path=local_parquet_path,
                    content_type="application/vnd.apache.parquet",
                ):
                    raise RuntimeError(f"Failed to upload GeoParquet to MinIO: {parquet_object_key}")
                created_minio_object = parquet_object_key

                # Upload PMTiles
                if not minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=pmtiles_object_key,
                    file_path=local_pmtiles_path,
                    content_type="application/vnd.pmtiles",
                ):
                    raise RuntimeError(f"Failed to upload PMTiles to MinIO: {pmtiles_object_key}")
                created_pmtiles_minio_object = pmtiles_object_key

                accessibility_type = AccessibilityType.Minio
                params = {
                    "bucket": bucket_name,
                    "object_name": parquet_object_key,
                    "pmtiles_object_name": pmtiles_object_key,
                    "primary_geometry_column": gdf.geometry.name,
                    "compression": compression,
                    "feature_count": len(gdf),
                    "columns": column_names,
                }

            # 3B. Persist to Local Media Directory
            else:
                permanent_parquet_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id), "parquet")
                permanent_tiles_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id), "tiles")
                os.makedirs(permanent_parquet_dir, exist_ok=True)
                os.makedirs(permanent_tiles_dir, exist_ok=True)

                stored_parquet_path = os.path.join(permanent_parquet_dir, parquet_filename)
                stored_pmtiles_path = os.path.join(permanent_tiles_dir, pmtiles_filename)

                shutil.copy2(local_parquet_path, stored_parquet_path)
                created_local_file = stored_parquet_path

                shutil.copy2(local_pmtiles_path, stored_pmtiles_path)
                created_pmtiles_local_file = stored_pmtiles_path

                accessibility_type = AccessibilityType.File
                params = {
                    "file_path": os.path.relpath(stored_parquet_path, MEDIA_BASE),
                    "pmtiles_file_path": os.path.relpath(stored_pmtiles_path, MEDIA_BASE),
                    "path_base": "media_rel",
                    "primary_geometry_column": gdf.geometry.name,
                    "compression": compression,
                    "feature_count": len(gdf),
                    "columns": column_names,
                }

        return EnginePersistenceResult(
            accessibility_type=accessibility_type,
            format_label="GeoParquet",
            params=params,
            created_minio_object=created_minio_object,
            created_local_file=created_local_file,
            created_pmtiles_minio_object=created_pmtiles_minio_object,
            created_pmtiles_local_file=created_pmtiles_local_file,
        )

    def rollback(
        self,
        *,
        session: Session,
        persistence_result: EnginePersistenceResult,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str,
    ) -> None:
        # 1. Rollback Parquet
        if persistence_result.created_minio_object and minio_manager:
            try:
                minio_manager.delete_object(bucket_name, persistence_result.created_minio_object)
            except Exception as e:
                da_logger.warning(f"Failed to rollback MinIO GeoParquet object {persistence_result.created_minio_object}: {e}")

        if persistence_result.created_local_file and os.path.exists(persistence_result.created_local_file):
            try:
                os.remove(persistence_result.created_local_file)
            except Exception as e:
                da_logger.warning(f"Failed to rollback local GeoParquet file: {e}")

        # 2. Rollback Companion PMTiles
        if persistence_result.created_pmtiles_minio_object and minio_manager:
            try:
                minio_manager.delete_object(bucket_name, persistence_result.created_pmtiles_minio_object)
            except Exception as e:
                da_logger.warning(f"Failed to rollback MinIO PMTiles object {persistence_result.created_pmtiles_minio_object}: {e}")

        if persistence_result.created_pmtiles_local_file and os.path.exists(persistence_result.created_pmtiles_local_file):
            try:
                os.remove(persistence_result.created_pmtiles_local_file)
            except Exception as e:
                da_logger.warning(f"Failed to rollback local PMTiles file: {e}")