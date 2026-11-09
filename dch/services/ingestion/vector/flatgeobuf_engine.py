# app/damapql/dch/services/ingestion/vector/flatgeobuf_engine.py
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, Optional

import geopandas as gpd
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


class FlatGeobufVectorEngine(BaseVectorStorageEngine):
    engine_type = VectorStorageEngineType.FLATGEOBUF

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
        filename = f"{safe_title}_{uuid_str}.fgb"

        with tempfile.TemporaryDirectory(prefix="dch_fgb_") as tmp_dir:
            local_tmp = os.path.join(tmp_dir, filename)

            # Ensure WGS84 coordinates if not already reprojected
            if gdf.crs and gdf.crs.to_epsg() != target_srid:
                gdf = gdf.to_crs(epsg=target_srid)

            # Generate FlatGeobuf with Packed Hilbert R-Tree for HTTP range requests
            gdf.to_file(local_tmp, driver="FlatGeobuf", spatial_index=True)

            column_names = [col for col in gdf.columns if col != gdf.geometry.name]
            created_minio_object = None
            created_local_file = None

            # -------------------------------------------------------------
            # Target A: MinIO Managed Storage
            # -------------------------------------------------------------
            if minio_manager is not None:
                object_key = f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/fgb/{filename}"
                uploaded = minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=object_key,
                    file_path=local_tmp,
                    content_type="application/octet-stream",
                )
                if not uploaded:
                    raise RuntimeError(f"Failed to upload FlatGeobuf to MinIO: {object_key}")

                created_minio_object = object_key
                accessibility_type = AccessibilityType.Minio
                params = {
                    "bucket": bucket_name,
                    "object_name": object_key,
                    "geometry_col": gdf.geometry.name,
                    "feature_count": len(gdf),
                    "columns": column_names,
                    "is_temporary": (extra_params or {}).get("is_temporary", False),
                }

            # -------------------------------------------------------------
            # Target B: Local File Storage Fallback
            # -------------------------------------------------------------
            else:
                permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id), "fgb")
                os.makedirs(permanent_dir, exist_ok=True)
                stored_path = os.path.join(permanent_dir, filename)
                shutil.copy2(local_tmp, stored_path)

                created_local_file = stored_path
                accessibility_type = AccessibilityType.File
                params = {
                    "file_path": os.path.relpath(stored_path, MEDIA_BASE),
                    "path_base": "media_rel",
                    "geometry_col": gdf.geometry.name,
                    "feature_count": len(gdf),
                    "columns": column_names,
                    "is_temporary": (extra_params or {}).get("is_temporary", False),
                }

        return EnginePersistenceResult(
            accessibility_type=accessibility_type,
            format_label="FlatGeobuf",
            params=params,
            created_minio_object=created_minio_object,
            created_local_file=created_local_file,
        )

    def rollback(
        self,
        *,
        session: Session,
        persistence_result: EnginePersistenceResult,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str,
    ) -> None:
        if persistence_result.created_minio_object and minio_manager:
            try:
                minio_manager.delete_object(bucket_name, persistence_result.created_minio_object)
                da_logger.info("Rollback: Removed MinIO object %s", persistence_result.created_minio_object)
            except Exception as e:
                da_logger.warning("Failed to rollback MinIO FlatGeobuf object: %s", e)

        if persistence_result.created_local_file and os.path.exists(persistence_result.created_local_file):
            try:
                os.remove(persistence_result.created_local_file)
                da_logger.info("Rollback: Removed local file %s", persistence_result.created_local_file)
            except Exception as e:
                da_logger.warning("Failed to rollback local FlatGeobuf file: %s", e)