# app/damapql/dch/services/ingestion/vector/flatgeobuf_engine.py
from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, Optional

import geopandas as gpd
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from sqlalchemy.orm import Session

from app.damapql.dch.api_enum import ModelType
from app.damapql.dch.models import AccessibilityType, LayerCategory
from app.damapql.dch.services.ingestion.vector.base import (
    BaseVectorStorageEngine,
    EnginePersistenceResult,
    VectorStorageEngineType,
    build_layer_columns_meta,
)
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import MEDIA_BASE


class FlatGeobufVectorEngine(BaseVectorStorageEngine):
    engine_type = VectorStorageEngineType.FLATGEOBUF

    def persist(
            self,
            *,
            session: Session,
            gdf: gpd.GeoDataFrame,
            title: str,
            safe_title: str,
            uuid_str: str,
            category: Optional[LayerCategory] = None,
            model_type: Optional[str] = None,
            minio_manager: Optional[MinioManager] = None,
            bucket_name: str = "dch",
            target_srid: int = 4326,
            extra_params: Optional[Dict[str, Any]] = None,
    ) -> EnginePersistenceResult:
        local_filename = f"{safe_title}_{uuid_str}.fgb"

        with tempfile.TemporaryDirectory(prefix="dch_fgb_") as tmp_dir:
            local_tmp = os.path.join(tmp_dir, local_filename)

            if gdf.crs and gdf.crs.to_epsg() != target_srid:
                gdf = gdf.to_crs(epsg=target_srid)

            # Generate FlatGeobuf with Packed Hilbert R-Tree for HTTP range requests
            gdf.to_file(local_tmp, driver="FlatGeobuf", spatial_index=True)

            columns_meta = build_layer_columns_meta(gdf, geom_col=gdf.geometry.name)
            created_minio_object = None
            created_local_file = None

            resolved_model_type = model_type or ModelType.Geom.name
            if category is not None:
                relative_key = LayerInfoService.build_layer_object_name(
                    category=category,
                    file_name=f"{title}.fgb",  # or .parquet / .gpkg
                    model_type=resolved_model_type,
                    uuid_str=uuid_str,
                )
            else:
                relative_key = DCHUtils.build_versioned_object_name(
                    file_name=f"{title}.fgb",
                    category_main="general",
                    model_type=resolved_model_type,
                    uuid_str=uuid_str,
                )

            # Target A: MinIO Managed Storage
            if minio_manager is not None:
                uploaded = minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=relative_key,
                    file_path=local_tmp,
                    content_type="application/octet-stream",
                )
                if not uploaded:
                    raise RuntimeError(f"Failed to upload FlatGeobuf to MinIO: {relative_key}")

                created_minio_object = relative_key
                accessibility_type = AccessibilityType.Minio
                params = {
                    "bucket": bucket_name,
                    "object_name": relative_key,
                    "geometry_col": gdf.geometry.name,
                    "feature_count": len(gdf),
                    "columns": columns_meta,
                    "is_temporary": (extra_params or {}).get("is_temporary", False),
                }

            # Target B: Local File Storage Fallback (identical relative layout under MEDIA_BASE)
            else:
                stored_path = os.path.join(MEDIA_BASE, *relative_key.split("/"))
                os.makedirs(os.path.dirname(stored_path), exist_ok=True)
                shutil.copy2(local_tmp, stored_path)

                created_local_file = stored_path
                accessibility_type = AccessibilityType.File
                params = {
                    "file_path": relative_key,
                    "path_base": "media_rel",
                    "geometry_col": gdf.geometry.name,
                    "feature_count": len(gdf),
                    "columns": columns_meta,
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
            bucket_name: str = "dch",
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