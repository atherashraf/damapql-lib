# app/damapql/dch/services/ingestion/vector/vector_ingest_service.py
from __future__ import annotations

import os
import tempfile
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import geopandas as gpd
from fastapi import HTTPException, UploadFile
import pyproj
from shapely.geometry import box
from shapely.ops import transform
from sqlalchemy.orm import Session

from app.damapql.dch.models import (
    DataModel,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
)
from app.damapql.dch.services.ingestion.staging_storage import (
    LocalStagingStorage,
    StagedFile,
    StagingStorage,
)
from app.damapql.dch.services.ingestion.vector.base import (
    BaseVectorStorageEngine,
    EnginePersistenceResult,
    VectorIngestResult,
    VectorStorageEngineType,
)
from app.damapql.dch.services.ingestion.vector.flatgeobuf_engine import FlatGeobufVectorEngine
from app.damapql.dch.services.ingestion.vector.geopackage_engine import GeoPackageVectorEngine
from app.damapql.dch.services.ingestion.vector.geoparquet_engine import GeoParquetVectorEngine
from app.damapql.dch.services.ingestion.vector.postgis_engine import PostgisVectorEngine
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import MINIO_BUCKET_NAME
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


class VectorIngestService:
    """
    Central Vector Ingestion Orchestrator for Data Catalog Hub (DCH).
    Supports PostGIS, FlatGeobuf, GeoParquet, and GeoPackage engines.
    """

    _ENGINES: Dict[VectorStorageEngineType, Type[BaseVectorStorageEngine]] = {
        VectorStorageEngineType.POSTGIS: PostgisVectorEngine,
        VectorStorageEngineType.FLATGEOBUF: FlatGeobufVectorEngine,
        VectorStorageEngineType.GEOPARQUET: GeoParquetVectorEngine,
        VectorStorageEngineType.GEOPACKAGE: GeoPackageVectorEngine,
    }

    @classmethod
    def get_engine(cls, engine_type: VectorStorageEngineType) -> BaseVectorStorageEngine:
        engine_cls = cls._ENGINES.get(engine_type)
        if not engine_cls:
            raise HTTPException(status_code=400, detail=f"Unsupported vector storage engine '{engine_type}'.")
        return engine_cls()

    @staticmethod
    def parse_temporal(temporal: Optional[str]) -> date:
        if not temporal:
            return date.today()
        try:
            return datetime.strptime(temporal, "%Y-%m-%d").date()
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.") from e

    @classmethod
    def unpack_vector_source(cls, source_path: str, extract_dir: str) -> str:
        ext = os.path.splitext(source_path)[1].lower()
        if ext == ".zip":
            with zipfile.ZipFile(source_path, "r") as archive:
                real_dir = os.path.realpath(extract_dir)
                for member in archive.infolist():
                    target = os.path.realpath(os.path.join(extract_dir, member.filename))
                    if not target.startswith(real_dir + os.sep) and target != real_dir:
                        raise HTTPException(status_code=400, detail="Zip contains illegal relative path elements.")
                archive.extractall(extract_dir)

            for priority_ext in [".parquet", ".geoparquet", ".fgb", ".gpkg", ".shp", ".geojson", ".json"]:
                for root, _, files in os.walk(extract_dir):
                    for file in files:
                        if file.lower().endswith(priority_ext):
                            return os.path.join(root, file)
            raise HTTPException(status_code=400, detail="ZIP archive contains no supported spatial vector files.")
        return source_path

    @staticmethod
    def read_and_validate_gdf(file_path: str) -> gpd.GeoDataFrame:
        try:
            if file_path.lower().endswith((".parquet", ".geoparquet")):
                gdf = gpd.read_parquet(file_path)
            else:
                gdf = gpd.read_file(file_path)
        except Exception as e:
            da_logger.exception(f"Failed to load vector file: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to read vector source: {str(e)}")

        if gdf.empty:
            raise HTTPException(status_code=400, detail="Vector dataset is empty.")
        if gdf.crs is None:
            raise HTTPException(status_code=400, detail="Vector CRS is missing. Georeferenced dataset with valid CRS is required.")

        gdf = gdf[gdf.geometry.notnull() & (~gdf.geometry.is_empty)]
        if gdf.empty:
            raise HTTPException(status_code=400, detail="Vector dataset contains no valid geometries.")
        return gdf

    @staticmethod
    def compute_extents(gdf: gpd.GeoDataFrame) -> Tuple[List[float], Optional[List[float]], int]:
        native_srid = gdf.crs.to_epsg() or 0
        gdf_4326 = gdf.to_crs(epsg=4326) if gdf.crs.to_epsg() != 4326 else gdf
        minx, miny, maxx, maxy = gdf_4326.total_bounds
        extent_4326 = [float(minx), float(miny), float(maxx), float(maxy)]

        extent_3857 = None
        try:
            clamped_miny = max(float(miny), -85.05112878)
            clamped_maxy = min(float(maxy), 85.05112878)
            clamped_box = box(float(minx), clamped_miny, float(maxx), clamped_maxy)
            project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
            box_3857 = transform(project, clamped_box)
            extent_3857 = [float(v) for v in box_3857.bounds]
        except Exception as e:
            da_logger.warning(f"Could not compute EPSG:3857 extent: {e}")

        return extent_4326, extent_3857, native_srid

    # =========================================================================
    # CORE INGESTION EXECUTION ENGINE
    # =========================================================================

    @classmethod
    def _execute_ingestion(
        cls,
        session: Session,
        *,
        source_path: str,
        title: str,
        category: LayerCategory,
        engine_type: VectorStorageEngineType,
        temporal_date: date,
        model_type: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        symbology: Optional[dict] = None,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        uuid_str = DCHUtils.get_uuid1()
        safe_title = DCHUtils.short_table_name(title, max_len=40)
        engine_instance = cls.get_engine(engine_type)
        persisted_result: Optional[EnginePersistenceResult] = None

        try:
            with tempfile.TemporaryDirectory(prefix="dch_vec_") as temp_dir:
                resolved_path = cls.unpack_vector_source(source_path, temp_dir)
                gdf = cls.read_and_validate_gdf(resolved_path)
                extent_wgs84, extent_3857, native_srid = cls.compute_extents(gdf)
                geom_types = list(gdf.geom_type.unique())
                feature_count = len(gdf)

                # Persist via selected engine
                persisted_result = engine_instance.persist(
                    session=session,
                    gdf=gdf,
                    safe_title=safe_title,
                    uuid_str=uuid_str,
                    category=category,
                    minio_manager=minio_manager,
                    bucket_name=bucket_name,
                    target_srid=4326,
                    extra_params=extra_engine_params,
                )

                # Catalog registration
                access = LayerAccessibility(
                    accessibility_type=persisted_result.accessibility_type.value,
                    params=persisted_result.params,
                )
                session.add(access)
                session.flush()

                layer_info = LayerInfo(
                    uuid=uuid_str,
                    app_label="dch",
                    title=title,
                    layer_name=safe_title,
                    srid=native_srid or 4326,
                    extent=extent_wgs84,
                    extent_3857=extent_3857,
                    data_model=DataModel.V.name,
                    model_type=model_type,
                    geom_type=geom_types,
                    uploaded_by=uploaded_by,
                    temporal_res=temporal_date,
                    category_id=category.id,
                    layer_accessibility=access,
                    format=persisted_result.format_label,
                    layer_styling=symbology,
                )
                session.add(layer_info)
                session.commit()

                da_logger.info(f"Vector ingestion succeeded: format={persisted_result.format_label}, uuid={uuid_str}")

                return VectorIngestResult(
                    uuid=layer_info.uuid,
                    layer_info_id=layer_info.id,
                    layer_access_id=access.id,
                    format=persisted_result.format_label,
                    engine=engine_type,
                    feature_count=feature_count,
                    geom_type=geom_types,
                    srid=native_srid or 4326,
                )

        except Exception as e:
            session.rollback()
            da_logger.exception(f"Vector ingestion failed: {e}")
            if persisted_result:
                engine_instance.rollback(
                    session=session,
                    persistence_result=persisted_result,
                    minio_manager=minio_manager,
                    bucket_name=bucket_name,
                )
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"Vector ingestion failed: {str(e)}")

    # =========================================================================
    # A. MULTIPART UPLOAD INGESTION
    # =========================================================================

    @classmethod
    def ingest_upload(
        cls,
        session: Session,
        *,
        vector_file: UploadFile,
        title: str,
        category_id: int,
        engine_type: VectorStorageEngineType = VectorStorageEngineType.FLATGEOBUF,
        temporal: Optional[str] = None,
        model_type: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        symbology: Optional[dict] = None,
        minio_manager: Optional[MinioManager] = None,
        staging_storage: Optional[StagingStorage] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        category = session.get(LayerCategory, category_id)
        if not category:
            category = session.query(LayerCategory).filter(LayerCategory.main == "Other").first()
            if not category:
                category = session.query(LayerCategory).first()

        temporal_date = cls.parse_temporal(temporal)
        staging_storage = staging_storage or LocalStagingStorage()
        staged_file: Optional[StagedFile] = None

        try:
            staged_file = staging_storage.stage_upload(upload_file=vector_file, filename=vector_file.filename)
            return cls._execute_ingestion(
                session=session,
                source_path=staged_file.local_path,
                title=title,
                category=category,
                engine_type=engine_type,
                temporal_date=temporal_date,
                model_type=model_type,
                uploaded_by=uploaded_by,
                symbology=symbology,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                extra_engine_params=extra_engine_params,
            )
        finally:
            if staged_file:
                try:
                    staging_storage.cleanup(staged_file)
                except Exception as cleanup_err:
                    da_logger.warning(f"Failed to cleanup staged vector file: {cleanup_err}")

    # =========================================================================
    # B. FILESYSTEM / PRE-STAGED PATH INGESTION (REQUIRED FOR BATCH PIPELINE)
    # =========================================================================

    @classmethod
    def ingest_from_path(
        cls,
        session: Session,
        *,
        vector_path: str,
        title: str,
        category_id: int,
        engine_type: VectorStorageEngineType = VectorStorageEngineType.FLATGEOBUF,
        temporal: Optional[str] = None,
        model_type: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        symbology: Optional[dict] = None,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        category = session.get(LayerCategory, category_id)
        if not category:
            category = session.query(LayerCategory).filter(LayerCategory.main == "Other").first()
            if not category:
                category = session.query(LayerCategory).first()

        if not vector_path or not os.path.exists(vector_path):
            raise HTTPException(status_code=404, detail=f"Source vector path not found: {vector_path}")

        temporal_date = cls.parse_temporal(temporal)

        return cls._execute_ingestion(
            session=session,
            source_path=vector_path,
            title=title,
            category=category,
            engine_type=engine_type,
            temporal_date=temporal_date,
            model_type=model_type,
            uploaded_by=uploaded_by,
            symbology=symbology,
            minio_manager=minio_manager,
            bucket_name=bucket_name,
            extra_engine_params=extra_engine_params,
        )