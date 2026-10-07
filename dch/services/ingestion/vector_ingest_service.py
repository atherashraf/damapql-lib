"""Vector Ingestion Service Module.

Provides end-to-end orchestration for vector spatial data ingestion into the
Data Catalog Hub (DCH). Manages payload unpacking, georeferencing validation,
bounding-box transformations, dual-target persistence, and spatial catalog registration.

Architecture Overview:
    1. Ingestion Modes:
        * Multipart Upload (ingest_upload): Stages archive or vector payloads from HTTP requests.
        * Filesystem Ingestion (ingest_from_path): Ingests pre-staged or batch-mounted vector assets.

    2. Dual Persistence Targets:
        * Relational OLTP / MVT Serving (PostGIS):
            Transforms geometries to EPSG:4326, generates unique isolated table instances,
            sanitizes column identifiers, and builds GiST spatial indices.
        * Spatial Lakehouse / Analytics (GeoParquet):
            Normalizes vector layers to Apache GeoParquet v1.0.0+ specification with
            columnar Snappy compression for zero-copy DuckDB/Arrow querying and S3 range requests.

    3. Atomic Lifecycle & Rollback:
        Ensures strict 1:1 parity across spatial data storage and catalog entities
        (`LayerInfo` and `LayerAccessibility`). Employs deterministic rollback routines
        to tear down generated PostGIS tables, MinIO objects, or local disk artifacts
        if catalog transactions fail.

Supported Formats:
    * ESRI Shapefile (zipped)
    * OGC GeoPackage (.gpkg)
    * GeoJSON / JSON (.geojson, .json)
    * FlatGeobuf (.fgb)
    * Apache GeoParquet (.parquet, .geoparquet)
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
from fastapi import HTTPException, UploadFile
from pydantic import BaseModel
import pyproj
from shapely.geometry import box
from shapely.ops import transform
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.models import (
    AccessibilityType,
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
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import (
    DCH_APP_NAME,
    MEDIA_BASE,
    MINIO_BUCKET_NAME,
)
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


# =============================================================================
# 1. DTOs & ACCESSIBILITY PARAMS
# =============================================================================

@dataclass
class VectorIngestResult:
    """Represents the outcome of a successful vector ingestion pipeline.

    Attributes:
        uuid: Unique deterministic identifier (UUIDv1) assigned to the catalog asset.
        layer_info_id: Primary key of the persisted LayerInfo catalog record.
        layer_access_id: Primary key of the corresponding LayerAccessibility entry.
        feature_count: Total number of valid geometric features committed.
        geom_type: Distinct Shapely/OGC geometry primitive types present in the layer
            (e.g., ['Polygon', 'MultiPolygon']).
        srid: Resolved Spatial Reference Identifier (EPSG integer code) of the dataset.
    """
    uuid: str
    layer_info_id: int
    layer_access_id: int
    feature_count: int
    geom_type: List[str]
    srid: int


class PostgisAccessibilityParams(BaseModel):
    """Metadata parameters describing vector accessibility within a PostGIS database.

    Attributes:
        table_name: Fully qualified or schema-isolated PostGIS destination table name.
        schema_name: PostgreSQL target schema containing the table. Defaults to 'public'.
        geom_column: Name of the active PostGIS Geometry/Geography column. Defaults to 'geom'.
        srid: Coordinate Reference System identifier configured on the PostGIS geometry.
        feature_count: Total number of spatial records indexed in the table.
    """
    table_name: str
    schema_name: str = "public"
    geom_column: str = "geom"
    srid: int = 4326
    feature_count: int


class GeoParquetAccessibilityParams(BaseModel):
    """Metadata parameters describing GeoParquet assets stored in MinIO or local media bases.

    Attributes:
        bucket: Name of the MinIO/S3 object storage bucket (if stored remotely).
        object_name: Fully qualified S3 object key path inside the bucket.
        file_path: Absolute or relative local path (if persisted to local media storage).
        path_base: Root path classification ('media_rel' or 'absolute').
        primary_geometry_column: Name of the GeoParquet primary geometry column. Defaults to 'geometry'.
        compression: Columnar codec applied to row groups. Defaults to 'snappy'.
        feature_count: Total record count in the Parquet dataset.
        columns: Non-geometry attribute column names preserved in the dataset schema.
    """
    bucket: Optional[str] = None
    object_name: Optional[str] = None
    file_path: Optional[str] = None
    path_base: Optional[str] = None
    primary_geometry_column: str = "geometry"
    compression: str = "snappy"
    feature_count: int
    columns: List[str]


# =============================================================================
# 2. VECTOR INGESTION SERVICE (GEOPARQUET + POSTGIS)
# =============================================================================

class VectorIngestService:
    """
    Orchestrates validation, transformation, persistence, and catalog registration for vector assets.

    Manages archive decompression, geometry validation, bounding box derivations
    (WGS84 and clamped Web Mercator), database/lakehouse ingestion, and transactional
    catalog metadata registration.
    """

    ALLOWED_EXTENSIONS = {".zip", ".geojson", ".json", ".gpkg", ".fgb", ".shp", ".parquet", ".geoparquet"}

    @staticmethod
    def parse_temporal(temporal: Optional[str]) -> date:
        """
        Parses an ISO date string into a Python date object with validation.

        Args:
            temporal: ISO 8601 formatted date string (YYYY-MM-DD). If None, defaults to current date.

        Returns:
            date: Resolved Python date object.

        Raises:
            HTTPException: 400 Bad Request if the input string violates YYYY-MM-DD formatting.
        """
        if not temporal:
            return date.today()
        try:
            return datetime.strptime(temporal, "%Y-%m-%d").date()
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail="Invalid temporal format. Use YYYY-MM-DD.",
            ) from e

    @staticmethod
    def build_unique_file_name(title: str, uuid_str: str, temporal: str, original_name: str) -> str:
        """
        Constructs a deterministic, collision-safe, filesystem-sanitized file name.

        Args:
            title: Human-readable layer title.
            uuid_str: Hexadecimal UUID string identifying the pipeline execution.
            temporal: Temporal date string formatted as YYYY-MM-DD.
            original_name: Original file name including suffix extension.

        Returns:
            str: Normalized file name in format `{safe_title}_{uuid}_{temporal}{ext}`.
        """
        file_ext = os.path.splitext(original_name)[1]
        safe_title = title.strip().replace("/", "_").replace("\\", "_").replace(" ", "_")
        return f"{safe_title}_{uuid_str}_{temporal}{file_ext}"

    @staticmethod
    def build_table_name(title: str, uuid_str: str) -> str:
        """
        Constructs a sanitized SQL identifier suitable for PostGIS table creation.
        Ensures compliance with PostgreSQL 63-character identifier limits by clipping
        the title slug and appending an abbreviated UUID suffix.

        Args:
            title: Human-readable layer title.
            uuid_str: Unique pipeline execution identifier.

        Returns:
            str: PostgreSQL-compatible table name (`vec_{title_prefix}_{uuid_suffix}`).
        """
        safe_title = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
        clean_uuid = uuid_str.replace("-", "_")
        return f"vec_{safe_title[:28]}_{clean_uuid[:18]}"

    @staticmethod
    def build_object_key(category: LayerCategory, filename: str) -> str:
        """
        Constructs an S3/MinIO hierarchical object key conforming to catalog namespaces.

        Args:
            category: SQLAlchemy LayerCategory model containing `main` and `sub` classifications.
            filename: Normalized target file name (e.g., `layer_uuid_2026-10-03.parquet`).

        Returns:
            str: Standardized S3 key formatted as `{app_name}/{main_cat}/{sub_cat}/{filename}`.
        """
        sub_cat = category.sub or "general"
        return f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/{filename}"

    @classmethod
    def unpack_vector_source(cls, source_path: str, extract_dir: str) -> str:
        """
        Resolves raw vector files or unpacks archive payloads into a target working directory.

        If a ZIP archive is supplied, it recursively extracts contents and scans for
        the primary supported vector file format based on extension priority.

        Args:
            source_path: Local filesystem path to the uploaded or staged vector payload.
            extract_dir: Temporary directory path where archives should be extracted.

        Returns:
            str: Absolute path to the resolved target vector file ready for driver loading.

        Raises:
            HTTPException: 400 Bad Request if the archive contains no supported vector files.
        """
        ext = os.path.splitext(source_path)[1].lower()

        if ext == ".zip":
            with zipfile.ZipFile(source_path, "r") as archive:
                real_extract_dir = os.path.realpath(extract_dir)
                for member in archive.infolist():
                    target_path = os.path.realpath(os.path.join(extract_dir, member.filename))
                    if not target_path.startswith(real_extract_dir + os.sep) and target_path != real_extract_dir:
                        raise HTTPException(status_code=400, detail="Zip file contains illegal relative path elements.")
                archive.extractall(extract_dir)

            for priority_ext in [".parquet", ".geoparquet", ".gpkg", ".shp", ".geojson", ".json", ".fgb"]:
                for root, _, files in os.walk(extract_dir):
                    for file in files:
                        if file.lower().endswith(priority_ext):
                            return os.path.join(root, file)
            raise HTTPException(
                status_code=400,
                detail="ZIP archive does not contain a supported vector file.",
            )

        return source_path

    @staticmethod
    def read_and_validate_gdf(file_path: str) -> gpd.GeoDataFrame:
        """
        Loads a vector data file into a GeoDataFrame, enforcing georeferencing and topology rules.

        Filters out NULL and empty geometric features, verifies CRS definitions,
        and validates tabular integrity.

        Args:
            file_path: Local filesystem path to the target vector source.

        Returns:
            gpd.GeoDataFrame: Cleaned, non-empty GeoDataFrame with defined coordinate reference system.

        Raises:
            HTTPException: 400 Bad Request if the dataset cannot be opened, has no CRS,
                contains zero features, or consists entirely of empty geometries.
        """
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
            raise HTTPException(
                status_code=400,
                detail="Vector CRS is missing. A georeferenced dataset with a valid CRS is required.",
            )

        gdf = gdf[gdf.geometry.notnull() & (~gdf.geometry.is_empty)]
        if gdf.empty:
            raise HTTPException(status_code=400, detail="Vector dataset contains no valid geometries.")

        return gdf

    @staticmethod
    def compute_extents(gdf: gpd.GeoDataFrame) -> Tuple[List[float], Optional[List[float]], int]:
        """
        Calculates WGS84 and Web Mercator (EPSG:3857) envelopes and resolves native SRID.

        Automatically handles bounding coordinates that exceed Web Mercator latitude
        limits ($\pm 85.05112878^\circ$) by clamping boundary boxes before reprojection.

        Args:
            gdf: Validated GeoPandas GeoDataFrame.

        Returns:
            Tuple[List[float], Optional[List[float]], int]:
                - extent_4326: Bounding box [minx, miny, maxx, maxy] in EPSG:4326 degrees.
                - extent_3857: Reprojected bounding box in Web Mercator meters, or None if transformation fails.
                - native_srid: Native integer EPSG SRID code (defaults to 0 if unresolvable).
        """
        native_srid = gdf.crs.to_epsg() or 0

        if gdf.crs.to_epsg() != 4326:
            gdf_4326 = gdf.to_crs(epsg=4326)
        else:
            gdf_4326 = gdf

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
            da_logger.warning(f"Could not compute 3857 extent: {e}")

        return extent_4326, extent_3857, native_srid

    @staticmethod
    def _rollback_vector_artifacts(
        *,
        session: Session,
        created_table_name: Optional[str] = None,
        schema_name: str = "public",
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = MINIO_BUCKET_NAME,
        created_minio_object: Optional[str] = None,
        created_local_file: Optional[str] = None,
    ) -> None:
        """
        Transactional rollback handler to clean up persisted storage artifacts upon aborted ingestion.

        Drops uncommitted or orphaned PostGIS tables and purges created MinIO objects
        or local filesystem files.

        Args:
            session: Active SQLAlchemy database session.
            created_table_name: Name of the PostGIS table to drop, if created.
            schema_name: PostgreSQL schema where the table resides.
            minio_manager: MinIO manager singleton instance (if MinIO is active).
            bucket_name: Target bucket where the object was uploaded.
            created_minio_object: S3 key of the uploaded Parquet file to purge.
            created_local_file: Local file path of written media files to delete.
        """
        if created_table_name:
            try:
                engine = session.get_bind()
                with engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{schema_name}"."{created_table_name}" CASCADE;'))
                da_logger.info(f"Dropped orphaned PostGIS table: {schema_name}.{created_table_name}")
            except Exception as e:
                da_logger.warning(f"Failed to drop PostGIS table {created_table_name}: {e}")

        if created_minio_object and minio_manager:
            try:
                minio_manager.delete_object(bucket_name, created_minio_object)
            except Exception as e:
                da_logger.warning(f"Failed to rollback MinIO GeoParquet object {created_minio_object}: {e}")

        if created_local_file and os.path.exists(created_local_file):
            try:
                os.remove(created_local_file)
            except Exception as e:
                da_logger.warning(f"Failed to rollback local GeoParquet file {created_local_file}: {e}")

    # =========================================================================
    # C. PERSISTENCE WORKFLOWS
    # =========================================================================

    @classmethod
    def persist_to_postgis(
        cls,
        session: Session,
        gdf: gpd.GeoDataFrame,
        table_name: str,
        schema_name: str = "public",
        target_srid: int = 4326,
    ) -> Tuple[str, List[str]]:
        """
        Persists a GeoDataFrame directly to a dedicated PostGIS table with spatial indexing.

        Standardizes column identifiers to snake_case alphanumeric strings, converts
        geometries to the target SRID, writes rows via SQLAlchemy engine, and creates
        a GiST index on the geometry column.

        Args:
            session: Active SQLAlchemy database session providing the engine bind.
            gdf: Validated GeoDataFrame to persist.
            table_name: Unique PostgreSQL identifier for the destination table.
            schema_name: Destination schema name. Defaults to 'public'.
            target_srid: Output coordinate reference system EPSG integer code. Defaults to 4326.

        Returns:
            Tuple[str, List[str]]:
                - table_name: Created PostGIS table name.
                - geom_types: Unique geometry primitive types found in the ingested table.
        """
        engine = session.get_bind()

        # Isolate transformations to a copy to avoid mutating caller data
        db_gdf = gdf.copy()

        if db_gdf.crs.to_epsg() != target_srid:
            db_gdf = db_gdf.to_crs(epsg=target_srid)

        geom_col = db_gdf.geometry.name
        db_gdf.columns = [
            "".join(c if c.isalnum() else "_" for c in col.lower()).strip("_")
            if col != geom_col
            else "geom"
            for col in db_gdf.columns
        ]

        geom_types = list(db_gdf.geom_type.unique())

        db_gdf.to_postgis(
            name=table_name,
            con=engine,
            schema=schema_name,
            if_exists="fail",
            index=True,
            index_label="fid",
        )

        idx_name = f"idx_{table_name[:48]}_geom"
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                    f'ON "{schema_name}"."{table_name}" USING GIST ("geom");'
                )
            )

        return table_name, geom_types

    @classmethod
    def persist_to_geoparquet(
        cls,
        gdf: gpd.GeoDataFrame,
        output_path: str,
        compression: str = "snappy",
    ) -> str:
        """
        Serializes a GeoDataFrame to standard Apache GeoParquet format.

        Encodes geometry columns in Well-Known Binary (WKB) format and embeds
        standardized `geo` metadata conforming to GeoParquet Specification v1.0.0.

        Args:
            gdf: Validated GeoDataFrame to serialize.
            output_path: Local filesystem destination path for the `.parquet` file.
            compression: Columnar compression algorithm ('snappy', 'gzip', 'zstd'). Defaults to 'snappy'.

        Returns:
            str: Destination file path of the generated GeoParquet dataset.
        """
        # Ensure CRS is clearly defined for parquet geo-metadata
        gdf.to_parquet(
            output_path,
            index=False,
            compression=compression,
            schema_version="1.0.0",
        )
        return output_path

    # =========================================================================
    # D. INGESTION PIPELINE: UPLOAD FILE
    # =========================================================================

    @classmethod
    def ingest_upload(
        cls,
        session: Session,
        minio_manager: Optional[MinioManager] = None,
        staging_storage: Optional[StagingStorage] = None,
        *,
        title: str,
        category_id: int,
        temporal: Optional[str],
        model_type: Optional[str],
        vector_file: UploadFile,
        uploaded_by: Optional[int],
        persist_to_db: bool = False,
        bucket_name: str = MINIO_BUCKET_NAME,
    ) -> VectorIngestResult:
        """
        Executes the multipart upload ingestion pipeline for incoming vector payloads.

        Orchestrates staging, format decompression, GeoPandas validation, CRS reprojection,
        spatial envelope calculations, persistence routing (PostGIS vs. GeoParquet Lakehouse),
        and 1:1 transactional registration in LayerInfo and LayerAccessibility catalogs.

        Args:
            session: Active SQLAlchemy session bound to the application transaction.
            minio_manager: MinioManager instance for object persistence. If None, falls back to local MEDIA_BASE.
            staging_storage: Staging strategy implementation for stream buffers. Defaults to LocalStagingStorage.
            title: Human-readable title for the vector layer catalog entry.
            category_id: Database identifier of the parent LayerCategory record.
            temporal: ISO 8601 temporal acquisition date string (YYYY-MM-DD). Defaults to current date.
            model_type: Optional domain model classification tag (e.g., 'HYDROLOGY', 'PARCELS').
            vector_file: Incoming FastAPI UploadFile multipart stream.
            uploaded_by: Database user ID of the authenticated user performing the upload.
            persist_to_db: If True, writes features to a dedicated PostGIS table.
                If False, serializes features to GeoParquet and persists to MinIO/Local storage.
            bucket_name: Destination S3 bucket when MinIO persistence is active.

        Returns:
            VectorIngestResult: Ingestion metadata containing layer UUID, catalog IDs, feature count,
                resolved SRID, and geometry types.

        Raises:
            HTTPException: 400 Bad Request on invalid metadata, missing category, or malformed geometries.
            HTTPException: 500 Internal Server Error on storage I/O, conversion, or transaction failures.
        """
        category = session.get(LayerCategory, category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID")
        if not vector_file.filename:
            raise HTTPException(status_code=400, detail="Vector file name is missing.")

        temporal_date = cls.parse_temporal(temporal)
        uuid_str = DCHUtils.get_uuid1()
        temporal_str = temporal or temporal_date.isoformat()
        unique_name = cls.build_unique_file_name(
            title=title, uuid_str=uuid_str, temporal=temporal_str, original_name=vector_file.filename
        )

        staging_storage = staging_storage or LocalStagingStorage()
        staged_file: Optional[StagedFile] = None

        created_table_name: Optional[str] = None
        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        try:
            staged_file = staging_storage.stage_upload(upload_file=vector_file, filename=unique_name)

            with tempfile.TemporaryDirectory(prefix="dch_vec_") as temp_dir:
                resolved_vector_path = cls.unpack_vector_source(staged_file.local_path, temp_dir)
                gdf = cls.read_and_validate_gdf(resolved_vector_path)
                extent_wgs84, extent_3857, native_srid = cls.compute_extents(gdf)
                geom_types = list(gdf.geom_type.unique())
                feature_count = len(gdf)
                layer_name = os.path.splitext(unique_name)[0]

                # Target 1: PostGIS Database Table
                if persist_to_db:
                    table_name = cls.build_table_name(title=title, uuid_str=uuid_str)
                    cls.persist_to_postgis(session=session, gdf=gdf, table_name=table_name, target_srid=4326)
                    created_table_name = table_name

                    accessibility_type = getattr(AccessibilityType, "Database", AccessibilityType.File)
                    accessibility_params = PostgisAccessibilityParams(
                        table_name=table_name,
                        schema_name="public",
                        geom_column="geom",
                        srid=4326,
                        feature_count=feature_count,
                    )
                    data_format = "PostGIS"

                # Target 2: GeoParquet Object Storage (MinIO or Local)
                else:
                    parquet_filename = f"{layer_name}.parquet"
                    temp_parquet_path = os.path.join(temp_dir, parquet_filename)
                    cls.persist_to_geoparquet(gdf, temp_parquet_path, compression="snappy")

                    column_names = [col for col in gdf.columns if col != gdf.geometry.name]

                    if minio_manager is not None:
                        object_key = cls.build_object_key(category, parquet_filename)
                        uploaded = minio_manager.upload_file(
                            bucket_name=bucket_name,
                            object_name=object_key,
                            file_path=temp_parquet_path,
                            content_type="application/vnd.apache.parquet",
                        )
                        if not uploaded:
                            raise HTTPException(status_code=500, detail="Failed to upload GeoParquet to MinIO.")

                        created_minio_object = object_key
                        accessibility_type = AccessibilityType.Minio
                        accessibility_params = GeoParquetAccessibilityParams(
                            bucket=bucket_name,
                            object_name=object_key,
                            primary_geometry_column=gdf.geometry.name,
                            compression="snappy",
                            feature_count=feature_count,
                            columns=column_names,
                        )
                    else:
                        permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id))
                        os.makedirs(permanent_dir, exist_ok=True)
                        stored_file_path = os.path.join(permanent_dir, parquet_filename)

                        shutil.copy2(temp_parquet_path, stored_file_path)
                        created_local_file = stored_file_path
                        accessibility_type = AccessibilityType.File
                        accessibility_params = GeoParquetAccessibilityParams(
                            file_path=stored_file_path,
                            path_base="media_rel",
                            primary_geometry_column=gdf.geometry.name,
                            compression="snappy",
                            feature_count=feature_count,
                            columns=column_names,
                        )

                    data_format = "GeoParquet"

                # Register 1:1 LayerAccessibility & LayerInfo
                params_dict = (
                    accessibility_params.model_dump(mode="json")
                    if isinstance(accessibility_params, BaseModel)
                    else accessibility_params
                )
                access = LayerAccessibility(accessibility_type=accessibility_type.value, params=params_dict)
                session.add(access)
                session.flush()

                layer_info = LayerInfo(
                    uuid=uuid_str,
                    app_label="dch",
                    title=title,
                    layer_name=layer_name,
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
                    quality_level=None,
                    format=data_format,
                )
                session.add(layer_info)
                session.flush()

                session.commit()
                da_logger.info(f"Vector GeoParquet ingestion completed: {uuid_str}")

                return VectorIngestResult(
                    uuid=layer_info.uuid,
                    layer_info_id=layer_info.id,
                    layer_access_id=layer_info.layer_access_id,
                    feature_count=feature_count,
                    geom_type=geom_types,
                    srid=native_srid or 4326,
                )

        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to ingest vector file: {e}")
            cls._rollback_vector_artifacts(
                session=session,
                created_table_name=created_table_name,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                created_minio_object=created_minio_object,
                created_local_file=created_local_file,
            )
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"Internal ingestion error: {str(e)}")

        finally:
            if staged_file:
                try:
                    staging_storage.cleanup(staged_file)
                except Exception as cleanup_error:
                    da_logger.warning(f"Failed to cleanup staged file: {cleanup_error}")