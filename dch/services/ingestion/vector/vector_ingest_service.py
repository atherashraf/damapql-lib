# app/damapql/dch/services/ingestion/vector/vector_ingest_service.py
from __future__ import annotations

import os
import tempfile
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import geopandas as gpd
import pyproj
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException, UploadFile
from shapely.geometry import box
from shapely.ops import transform
from sqlalchemy.orm import Session

from app.damapql.dch.api_enum import ModelType
from app.damapql.dch.models import (DataModel, LayerAccessibility, LayerCategory, LayerInfo, )
from app.damapql.dch.services.ingestion.staging_storage import (LocalStagingStorage, StagedFile, StagingStorage, )
from app.damapql.dch.services.ingestion.vector.base import (BaseVectorStorageEngine, VectorIngestResult,
                                                            VectorPersistSummary, VectorStorageEngineType, )
from app.damapql.dch.services.ingestion.vector.flatgeobuf_engine import FlatGeobufVectorEngine
from app.damapql.dch.services.ingestion.vector.geopackage_engine import GeoPackageVectorEngine
from app.damapql.dch.services.ingestion.vector.geoparquet_engine import GeoParquetVectorEngine
from app.damapql.dch.services.ingestion.vector.postgis_engine import PostgisVectorEngine
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import MINIO_BUCKET_NAME

BUFFER_SIZE = 1024 * 1024  # 1 MB chunk buffer


class VectorIngestService:
    """
    Generic Vector Ingestion Orchestrator for DAMapQL.
    Supports PostGIS, FlatGeobuf, GeoParquet, and GeoPackage engines across
    permanent LayerInfo catalogs, batch pipelines, and temporary layer routers.
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
    def stage_upload_bundle(cls, files: List[UploadFile], staging_storage: StagingStorage) -> Tuple[str, List[StagedFile]]:
        """Streams single or multi-file shapefile bundles (.shp, .shx, .dbf, .prj) into one staging dir."""
        valid_files = [f for f in files if f and f.filename]
        if not valid_files:
            raise HTTPException(status_code=400, detail="No vector files uploaded.")

        staged_records: List[StagedFile] = []
        first_name = os.path.basename(valid_files[0].filename)
        base_staged = staging_storage.stage_upload(valid_files[0], first_name)
        staged_records.append(base_staged)
        bundle_dir = base_staged.staging_dir or os.path.dirname(base_staged.local_path)

        primary_exts = (".shp", ".zip", ".fgb", ".parquet", ".geoparquet", ".gpkg", ".geojson", ".json")
        main_path: Optional[str] = (
            base_staged.local_path if base_staged.local_path.lower().endswith(primary_exts) else None
        )

        for uf in valid_files[1:]:
            safe_name = os.path.basename(uf.filename)
            dest_path = os.path.join(bundle_dir, safe_name)
            uf.file.seek(0)
            with open(dest_path, "wb") as dst:
                while chunk := uf.file.read(BUFFER_SIZE):
                    dst.write(chunk)
            if safe_name.lower().endswith(primary_exts):
                main_path = dest_path

        if not main_path:
            raise HTTPException(
                status_code=400,
                detail="Missing primary spatial file (.shp, .zip, .geojson, .fgb, .gpkg, or .parquet).",
            )

        return main_path, staged_records

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
    def read_and_validate_gdf(
        file_path: str,
        *,
        require_crs: bool = True,
        enforce_single_geom: bool = True,
    ) -> Tuple[gpd.GeoDataFrame, str, List[str]]:
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
            if require_crs:
                raise HTTPException(
                    status_code=400,
                    detail="Vector CRS is missing (.prj required). Georeferenced dataset with valid CRS is required.",
                )
            gdf.set_crs(epsg=4326, inplace=True)

        gdf = gdf[gdf.geometry.notnull() & (~gdf.geometry.is_empty)].copy()
        if gdf.empty:
            raise HTTPException(status_code=400, detail="Vector dataset contains no valid geometries.")

        raw_geom_types = sorted(gdf.geom_type.dropna().unique().tolist())

        families = set()
        for gt in raw_geom_types:
            if "Point" in gt:
                families.add("Point")
            elif "Line" in gt:
                families.add("LineString")
            elif "Polygon" in gt:
                families.add("Polygon")
            else:
                families.add(gt)

        if enforce_single_geom and len(families) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"Mixed geometry types are not allowed ({', '.join(raw_geom_types)}). Upload a single-geometry shapefile.",
            )

        primary_geom = next(iter(families)) if families else "Polygon"
        gdf.columns = gdf.columns.str.lower()
        gdf.rename_geometry("geom", inplace=True)

        return gdf, primary_geom, raw_geom_types

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
    # A. GENERIC PERSISTENCE (Reusable by TempLayerInfo or any custom router)
    # =========================================================================

    @classmethod
    def persist_vector(
        cls,
        session: Session,
        *,
        source_path: str,
        title: str,
        engine_type: VectorStorageEngineType = VectorStorageEngineType.FLATGEOBUF,
        category: Optional[LayerCategory] = None,
        model_type: Optional[str] = None,
        uuid_str: Optional[str] = None,
        require_crs: bool = True,
        enforce_single_geom: bool = True,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorPersistSummary:
        uuid_val = uuid_str or DCHUtils.get_uuid1()
        safe_title = DCHUtils.short_table_name(title, max_len=40)
        engine_instance = cls.get_engine(engine_type)

        with tempfile.TemporaryDirectory(prefix="dch_vec_") as temp_dir:
            resolved_path = cls.unpack_vector_source(source_path, temp_dir)
            gdf, primary_geom, raw_geom_types = cls.read_and_validate_gdf(
                resolved_path,
                require_crs=require_crs,
                enforce_single_geom=enforce_single_geom,
            )
            extent_wgs84, extent_3857, native_srid = cls.compute_extents(gdf)

            persisted = engine_instance.persist(
                session=session,
                gdf=gdf,
                title=title,
                safe_title=safe_title,
                uuid_str=uuid_val,
                category=category,
                model_type=model_type or ModelType.Geom.name,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                target_srid=4326,
                extra_params=extra_engine_params,
            )

            return VectorPersistSummary(
                uuid=uuid_val,
                layer_name=persisted.created_table_name or safe_title,
                format=persisted.format_label,
                engine=engine_type,
                accessibility_type=persisted.accessibility_type,
                access_params=persisted.params,
                feature_count=int(len(gdf)),
                geom_type=raw_geom_types,
                primary_geom_type=primary_geom,
                srid=native_srid or 4326,
                extent=extent_wgs84,
                extent_3857=extent_3857,
                persistence_result=persisted,
            )

    # =========================================================================
    # B. CATALOG EXECUTION ENGINE (LayerAccessibility + LayerInfo)
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
        require_crs: bool = True,
        enforce_single_geom: bool = True,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        engine_instance = cls.get_engine(engine_type)
        summary: Optional[VectorPersistSummary] = None

        try:
            summary = cls.persist_vector(
                session=session,
                source_path=source_path,
                title=title,
                engine_type=engine_type,
                category=category,
                model_type=model_type,
                require_crs=require_crs,
                enforce_single_geom=enforce_single_geom,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                extra_engine_params=extra_engine_params,
            )

            access = LayerAccessibility(
                accessibility_type=summary.accessibility_type.value,
                params=summary.access_params,
            )
            session.add(access)
            session.flush()

            if summary.persistence_result.created_table_name:
                layer_name = summary.persistence_result.created_table_name
            elif summary.persistence_result.created_minio_object:
                layer_name = os.path.splitext(
                    os.path.basename(summary.persistence_result.created_minio_object)
                )[0]
            elif summary.persistence_result.created_local_file:
                layer_name = os.path.splitext(
                    os.path.basename(summary.persistence_result.created_local_file)
                )[0]
            else:
                layer_name = f"{summary.layer_name}_{summary.uuid[:8]}"

            layer_info = LayerInfo(
                uuid=summary.uuid,
                app_label="dch",
                title=title.strip(),
                layer_name=layer_name,
                srid=4326 if engine_type == VectorStorageEngineType.POSTGIS else summary.srid,
                extent=summary.extent,
                extent_3857=summary.extent_3857,
                data_model=DataModel.V.name,
                model_type=model_type or ModelType.Geom.name,
                geom_type=summary.geom_type,
                uploaded_by=uploaded_by,
                temporal_res=temporal_date,
                category_id=category.id,
                layer_accessibility=access,
                format=summary.format,
                layer_styling=symbology,
            )
            session.add(layer_info)
            session.commit()

            da_logger.info(f"Vector ingestion succeeded: format={summary.format}, uuid={summary.uuid}")

            return VectorIngestResult(
                uuid=layer_info.uuid,
                layer_info_id=layer_info.id,
                layer_access_id=access.id,
                format=summary.format,
                engine=engine_type,
                feature_count=summary.feature_count,
                geom_type=summary.geom_type,
                srid=layer_info.srid or 4326,
                table_name=summary.persistence_result.created_table_name,
            )
        except Exception as exc:
            da_logger.exception(f"Vector ingestion failed: {exc}")
            # 1. Clean up persisted artifacts (PostGIS table in 'data' DB, MinIO objects, etc.)
            if summary and summary.persistence_result:
                try:
                    engine_instance.rollback(
                        session=session,
                        persistence_result=summary.persistence_result,
                        minio_manager=minio_manager,
                        bucket_name=bucket_name,
                    )
                except Exception as cleanup_err:
                    da_logger.warning(f"Engine rollback cleanup failed: {cleanup_err}")
            # 2. Safely rollback or invalidate the 'dch' metadata session
            try:
                session.rollback()
            except Exception:
                session.invalidate()
            raise

    @classmethod
    def _resolve_category(cls, session: Session, category_id: int) -> LayerCategory:
        category = session.get(LayerCategory, int(category_id))
        if not category:
            category = session.query(LayerCategory).filter(LayerCategory.main == "Other").first()
        if not category:
            category = session.query(LayerCategory).first()
        if not category:
            raise HTTPException(status_code=400, detail="Valid category_id is required.")
        return category

    # =========================================================================
    # C. MULTIPART UPLOAD INGESTION (Single file OR .shp/.shx/.dbf/.prj bundle)
    # =========================================================================

    @classmethod
    def ingest_upload(
        cls,
        session: Session,
        *,
        files: Optional[List[UploadFile]] = None,
        vector_file: Optional[UploadFile] = None,
        title: str,
        category_id: int,
        engine_type: VectorStorageEngineType = VectorStorageEngineType.POSTGIS,
        temporal: Optional[str] = None,
        model_type: Optional[str] = None,
        uploaded_by: Optional[int] = None,
        symbology: Optional[dict] = None,
        require_crs: bool = True,
        enforce_single_geom: bool = True,
        minio_manager: Optional[MinioManager] = None,
        staging_storage: Optional[StagingStorage] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        upload_list = list(files or [])
        if vector_file is not None:
            upload_list.append(vector_file)

        category = cls._resolve_category(session, category_id)
        temporal_date = cls.parse_temporal(temporal)
        staging = staging_storage or LocalStagingStorage()
        staged_files: List[StagedFile] = []

        try:
            main_path, staged_files = cls.stage_upload_bundle(upload_list, staging)
            return cls._execute_ingestion(
                session=session,
                source_path=main_path,
                title=title,
                category=category,
                engine_type=engine_type,
                temporal_date=temporal_date,
                model_type=model_type,
                uploaded_by=uploaded_by,
                symbology=symbology,
                require_crs=require_crs,
                enforce_single_geom=enforce_single_geom,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                extra_engine_params=extra_engine_params,
            )
        finally:
            for sf in staged_files:
                try:
                    staging.cleanup(sf)
                except Exception as cleanup_err:
                    da_logger.warning(f"Failed to cleanup staged vector file: {cleanup_err}")

    # =========================================================================
    # D. FILESYSTEM / PRE-STAGED PATH INGESTION (For PipelineIngestService)
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
        require_crs: bool = True,
        enforce_single_geom: bool = True,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        extra_engine_params: Optional[Dict[str, Any]] = None,
    ) -> VectorIngestResult:
        if not vector_path or not os.path.exists(vector_path):
            raise HTTPException(status_code=404, detail=f"Source vector path not found: {vector_path}")

        category = cls._resolve_category(session, category_id)
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
            require_crs=require_crs,
            enforce_single_geom=enforce_single_geom,
            minio_manager=minio_manager,
            bucket_name=bucket_name,
            extra_engine_params=extra_engine_params,
        )