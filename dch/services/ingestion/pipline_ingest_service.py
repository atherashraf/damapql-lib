"""Pipeline Ingestion Service Module.

Provides unified gateway orchestration for declarative, JSON-driven data ingestion
into the Data Catalog Hub (DCH). Ingests spatial, tabular, and unstructured document assets
with multi-target persistence routing.

Architecture Overview:
    1. Declarative Batch Payloads:
        * Accepts structured JSON requests (`PipelineBatchRequest`) containing multi-layer
          definitions, persistence preferences, and metadata mappings.
        * Supports both external storage references (`source_path`) and direct in-memory
          tabular payloads (`inline_records`).

    2. Polymorphic Asset Ingestion:
        * Raster Spatial Data (`R`):
            Delegates to `RasterIngestService` to normalize GeoTIFFs/VRTs into Cloud Optimized
            GeoTIFFs (COG) with georeferencing validation and spatial envelope generation.
        * Vector Spatial Data (`V`):
            Delegates to `VectorIngestService` to persist vector datasets into PostGIS tables
            (with GiST indexing) or as Apache GeoParquet lakehouse assets in MinIO/local disk.
        * Tabular & Telemetry Payloads (`T`):
            Dynamically converts JSON dictionaries/arrays, CSVs, or telemetry time-series into
            strongly-typed Apache Parquet tables using PyArrow/Pandas, embedding schema definitions
            and row counts into catalog metadata.
        * Unstructured Documents (`D`):
            Handles PDFs, Excel sheets, technical memos, and shapefile archives with automatic
            MIME detection, file size tracking, and artifact persistence.

    3. Fault Tolerance & Transaction Management:
        * Non-Atomic Execution: Processes items independently; failures are captured in
          per-item summary reports without aborting subsequent tasks.
        * Atomic Execution: Wraps batch pipelines in SQLAlchemy savepoints (`session.begin_nested()`)
          to guarantee complete transactional rollback across both the catalog database and
          object storage (MinIO/local disk) if any layer fails.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.damapql.dch.dtypes import FileAccessibilityParams, MinioAccessibilityParams
from app.damapql.dch.models import (
    AccessibilityType,
    DataModel,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
)
from app.damapql.dch.services.ingestion.raster_ingest_service import (
    RasterIngestResult,
    RasterIngestService,
)
# from app.damapql.dch.services.ingestion.vector_ingest_service import (
#     VectorIngestResult,
#     VectorIngestService,
# )
from app.damapql.dch.services.ingestion.vector import (
    VectorIngestResult,
    VectorIngestService,
    VectorStorageEngineType,
)
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import DCH_APP_NAME, MEDIA_BASE, MINIO_BUCKET_NAME
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


# =============================================================================
# 1. ENUMS & PYDANTIC SPECIFICATIONS
# =============================================================================

class PipelineDataModel(str, Enum):
    """Enumeration of supported spatial and non-spatial catalog models."""
    RASTER = "R"
    VECTOR = "V"
    TABULAR = "T"
    DOCUMENT = "D"


class TargetPersistence(str, Enum):
    """Storage persistence target mode.

    Attributes:
        DATABASE: Writes features/records directly to relational database tables
            (e.g., PostGIS for vector layers).
        LAKEHOUSE: Persists normalized binary artifacts (COG, GeoParquet, Parquet,
            or raw documents) to MinIO object storage or local filesystem storage.
    """
    DATABASE = "database"
    LAKEHOUSE = "lakehouse"


class CategorySpec(BaseModel):
    """Declarative specification for catalog taxonomy categorization.

    Attributes:
        id: Direct database primary key of an existing `LayerCategory`.
        main: Top-level classification hierarchy name (e.g., 'Irrigation', 'Remote Sensing').
        sub: Secondary subdivision taxonomy name. Defaults to 'general'.
    """
    id: Optional[int] = None
    main: Optional[str] = None
    sub: Optional[str] = "general"


class PipelineLayerPayload(BaseModel):
    """Declarative specification for an individual spatial, tabular, or document layer.

    Attributes:
        title: Human-readable layer title for catalog indexing.
        category: Taxonomy category specification (by primary key or hierarchy name).
        source_path: Local filesystem path, network mount, or URI to the source asset.
        inline_records: Direct JSON dictionary or list of records to convert to Parquet.
        data_model: Explicit data model identifier ('R', 'V', 'T', 'D'). Inferred if omitted.
        temporal: Acquisition or reference date formatted as YYYY-MM-DD. Defaults to current date.
        model_type: Optional domain categorization string (e.g., 'HYDROLOGY', 'TELEMETRY').
        target: Storage target policy ('database' or 'lakehouse'). Defaults to 'lakehouse'.
        metadata_extra: Arbitrary key-value metadata to attach to the catalog registration.
    """
    title: str = Field(..., min_length=2, max_length=255, description="Layer title.")
    category: CategorySpec = Field(..., description="Target category specification.")
    source_path: Optional[str] = Field(None, description="Filesystem path or URI to the input file.")
    inline_records: Optional[Union[List[Dict[str, Any]], Dict[str, Any]]] = Field(
        None, description="Raw JSON array or dictionary to convert to Parquet."
    )
    data_model: Optional[PipelineDataModel] = Field(None, description="Data model identifier. If None, inferred automatically.")
    temporal: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO 8601 temporal acquisition date (YYYY-MM-DD).")
    model_type: Optional[str] = Field(None, description="Domain taxonomy classification tag.")
    target: TargetPersistence = Field(default=TargetPersistence.LAKEHOUSE, description="Storage persistence routing destination.")
    # Optional explicit engine override for Vector layers (postgis, flatgeobuf, geoparquet, geopackage)
    vector_engine: Optional[VectorStorageEngineType] = Field(default=None, description="Direct vector engine override.")
    metadata_extra: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Supplemental metadata attributes.")

    @model_validator(mode="after")
    def validate_source_presence(self) -> PipelineLayerPayload:
        if not self.source_path and self.inline_records is None:
            raise ValueError("Either 'source_path' or 'inline_records' must be supplied.")
        return self


class PipelineBatchRequest(BaseModel):
    """Root JSON specification payload for running batch pipeline ingestion.

    Attributes:
        pipeline_name: Unique orchestration run identifier or audit tag.
        atomic: When True, aborts and rolls back all layers if any single item fails.
            When False, ingests successfully processed layers and logs individual failures.
        bucket_name: MinIO/S3 bucket destination for lakehouse artifacts.
        layers: Non-empty list of layer payload definitions to execute.
    """
    pipeline_name: str = Field(..., description="Identifier for this pipeline run.")
    atomic: bool = Field(default=False, description="Whether to enforce atomic transaction boundaries across all layers.")
    bucket_name: str = Field(default=MINIO_BUCKET_NAME, description="Destination object storage bucket name.")
    layers: List[PipelineLayerPayload] = Field(..., min_length=1, description="Collection of layer specifications to ingest.")


# =============================================================================
# 2. ACCESSIBILITY PARAMS & DTOs
# =============================================================================

class TabularAccessibilityParams(BaseModel):
    """Metadata parameters describing Parquet-converted tabular and telemetry datasets.

    Attributes:
        bucket: MinIO bucket containing the Parquet artifact (if stored remotely).
        object_name: S3 object key path inside the bucket.
        file_path: Local filesystem path (if persisted to local media storage).
        path_base: Media base classification ('media_rel' or 'absolute').
        row_count: Total number of records/rows in the tabular dataset.
        columns: List of sanitized column names in the schema.
        compression: Columnar codec applied to row groups (e.g., 'snappy').
        schema_definition: Mapping of column names to PyArrow data type strings.
    """
    bucket: Optional[str] = None
    object_name: Optional[str] = None
    file_path: Optional[str] = None
    path_base: Optional[str] = None
    row_count: int
    columns: List[str]
    compression: str = "snappy"
    schema_definition: Dict[str, str] = Field(default_factory=dict)


class DocumentAccessibilityParams(BaseModel):
    """Metadata parameters describing unstructured document files.

    Attributes:
        bucket: MinIO bucket containing the document (if stored remotely).
        object_name: S3 object key path inside the bucket.
        file_path: Local filesystem path (if persisted to local media storage).
        path_base: Media base classification ('media_rel' or 'absolute').
        mime_type: Standardized MIME content-type string.
        file_size_bytes: Size of the asset on disk/storage in bytes.
        original_extension: File extension suffix including leading dot (e.g., '.pdf').
    """
    bucket: Optional[str] = None
    object_name: Optional[str] = None
    file_path: Optional[str] = None
    path_base: Optional[str] = None
    mime_type: str
    file_size_bytes: int
    original_extension: str


@dataclass
class LayerIngestSummary:
    """Ingestion execution summary for an individual layer item.

    Attributes:
        title: Title of the ingested layer.
        status: Execution status indicator ('SUCCESS' or 'FAILED').
        data_model: Catalog model type assigned ('R', 'V', 'T', 'D').
        uuid: Unique UUID assigned to the catalog asset on success.
        layer_info_id: Primary key of the generated `LayerInfo` record.
        layer_access_id: Primary key of the generated `LayerAccessibility` record.
        records_count: Total features (Vector), rows (Tabular), or bytes (Document).
        format: Standard storage format descriptor (e.g., 'COG', 'GeoParquet', 'PARQUET', 'PDF').
        error_message: Formatted error detail string if processing failed.
    """
    title: str
    status: Literal["SUCCESS", "FAILED"]
    data_model: str
    uuid: Optional[str] = None
    layer_info_id: Optional[int] = None
    layer_access_id: Optional[int] = None
    records_count: Optional[int] = None
    format: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PipelineBatchResult:
    """Aggregated outcome of an entire batch pipeline run.

    Attributes:
        pipeline_name: Identifier of the orchestration run.
        total_requested: Total number of layers submitted in the request.
        successful_count: Number of layers ingested and cataloged successfully.
        failed_count: Number of layers that failed processing.
        results: Detailed per-layer execution summaries.
    """
    pipeline_name: str
    total_requested: int
    successful_count: int
    failed_count: int
    results: List[LayerIngestSummary] = field(default_factory=list)


# =============================================================================
# 3. PIPELINE INGESTION SERVICE
# =============================================================================

class PipelineIngestService:
    """Unified Ingestion Gateway for Raster, Vector, Tabular (JSON to Parquet), and Document assets.

    Orchestrates automated format detection, taxonomy mapping, data normalization,
    and storage persistence across MinIO, local filesystems, and PostGIS.
    """

    RASTER_EXT = {".tif", ".tiff", ".vrt"}
    VECTOR_EXT = {".zip", ".shp", ".gpkg", ".geojson", ".fgb", ".geoparquet"}
    TABULAR_EXT = {".json", ".jsonl", ".csv", ".parquet"}
    DOCUMENT_EXT = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".txt", ".md", ".xml", ".zip"}

    # =========================================================================
    # A. RESOLUTION & CLASSIFICATION HELPERS
    # =========================================================================

    @classmethod
    def resolve_data_model(
        cls,
        source_path: Optional[str],
        inline_records: Optional[Union[List[Any], Dict[str, Any]]],
        explicit: Optional[str] = None,
    ) -> str:
        """Determines the appropriate DataModel code ('R', 'V', 'T', 'D').

        Resolves the classification based on explicit model tags, the presence of
        inline records, or by parsing the file extension of the source path.

        Args:
            source_path: Filesystem path or URI of the input file.
            inline_records: In-memory JSON records if provided.
            explicit: Explicit user-specified data model string.

        Returns:
            str: DataModel single-character string code ('R', 'V', 'T', or 'D').

        Raises:
            HTTPException: 400 Bad Request if the model cannot be inferred.
        """
        if explicit:
            return explicit.upper()

        if inline_records is not None:
            return DataModel.T.name

        if not source_path:
            raise HTTPException(status_code=400, detail="Unable to infer data model without source.")

        ext = os.path.splitext(source_path.strip().lower())[1]

        if ext in cls.RASTER_EXT:
            return DataModel.R.name
        elif ext in cls.VECTOR_EXT:
            return DataModel.V.name
        elif ext in cls.TABULAR_EXT:
            return DataModel.T.name
        elif ext in cls.DOCUMENT_EXT:
            return DataModel.D.name

        return DataModel.D.name

    @staticmethod
    def resolve_category_id(session: Session, category_spec: Any) -> int:
        """Resolves a LayerCategory database primary key from a specification object.

        Checks direct primary key references first, then hierarchical name lookups
        (`main` and `sub`), and falls back to the default category named 'Other'.

        Args:
            session: Active SQLAlchemy database session.
            category_spec: CategorySpec instance or dict with category metadata.

        Returns:
            int: Primary key ID of the resolved `LayerCategory`.

        Raises:
            HTTPException: 400 Bad Request if the category cannot be resolved and no fallback exists.
        """
        if getattr(category_spec, "id", None) is not None:
            cat = session.get(LayerCategory, category_spec.id)
            if cat:
                return cat.id

        main_cat = getattr(category_spec, "main", None)
        sub_cat = getattr(category_spec, "sub", "general")

        if main_cat:
            query = session.query(LayerCategory).filter(LayerCategory.main.ilike(main_cat.strip()))
            if sub_cat:
                query = query.filter(LayerCategory.sub.ilike(sub_cat.strip()))
            cat = query.first()
            if cat:
                return cat.id

        fallback = session.query(LayerCategory).filter(LayerCategory.main == "Other").first()
        if fallback:
            return fallback.id

        raise HTTPException(status_code=400, detail=f"Category cannot be resolved: {category_spec}")

    @staticmethod
    def build_object_key(category: LayerCategory, filename: str) -> str:
        """Constructs an S3/MinIO standardized object storage key.

        Args:
            category: SQLAlchemy `LayerCategory` model instance.
            filename: Normalized target file name.

        Returns:
            str: S3 key formatted as `{app_name}/{main_cat}/{sub_cat}/{filename}`.
        """
        sub_cat = category.sub or "general"
        return f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/{filename}"

    # =========================================================================
    # B. TABULAR / JSON -> PARQUET CONVERSION HANDLER
    # =========================================================================

    @classmethod
    def process_tabular_json(
        cls,
        session: Session,
        *,
        title: str,
        category: LayerCategory,
        temporal_date: date,
        model_type: Optional[str],
        source_path: Optional[str],
        inline_records: Optional[Union[List[Any], Dict[str, Any]]],
        uploaded_by: Optional[int],
        minio_manager: Optional[MinioManager],
        bucket_name: str,
    ) -> LayerIngestSummary:
        """Converts tabular or JSON data into Apache Parquet format and catalogs it.

        Supports in-memory JSON objects/arrays, external JSON/JSONL files, CSVs,
        and pre-existing Parquet files. Standardizes column names, applies Snappy
        compression, persists the artifact, and registers 1:1 catalog records.

        Args:
            session: Active SQLAlchemy database session.
            title: Human-readable layer title.
            category: Resolved `LayerCategory` database model.
            temporal_date: Python date object representing temporal acquisition.
            model_type: Domain taxonomy tag.
            source_path: Filesystem path to the input tabular file (if applicable).
            inline_records: Raw JSON records (if provided in payload).
            uploaded_by: Database ID of the user triggering ingestion.
            minio_manager: MinIO manager singleton instance (if MinIO is active).
            bucket_name: Target MinIO/S3 bucket name.

        Returns:
            LayerIngestSummary: Ingestion details including row count and Parquet format.

        Raises:
            HTTPException: 400 Bad Request on empty or unparseable tabular data.
            HTTPException: 404 Not Found if `source_path` does not exist.
            HTTPException: 500 Internal Server Error on storage or database failure.
        """
        uuid_str = DCHUtils.get_uuid1()
        safe_title = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
        parquet_filename = f"{safe_title}_{uuid_str[:8]}_{temporal_date.isoformat()}.parquet"

        if inline_records is not None:
            records = inline_records if isinstance(inline_records, list) else [inline_records]
            df = pd.json_normalize(records)
        else:
            if not source_path or not os.path.exists(source_path):
                raise HTTPException(status_code=404, detail=f"Source file not found: {source_path}")

            ext = os.path.splitext(source_path)[1].lower()
            if ext in (".json", ".jsonl"):
                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                records = data if isinstance(data, list) else [data]
                df = pd.json_normalize(records)
            elif ext == ".csv":
                df = pd.read_csv(source_path)
            elif ext == ".parquet":
                df = pd.read_parquet(source_path)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported tabular extension: {ext}")

        if df.empty:
            raise HTTPException(status_code=400, detail="Tabular dataset contains 0 records.")

        df.columns = ["".join(c if c.isalnum() else "_" for c in col.lower()).strip("_") for col in df.columns]
        row_count = len(df)
        columns_list = df.columns.tolist()
        arrow_table = pa.Table.from_pandas(df)
        schema_dict = {field.name: str(field.type) for field in arrow_table.schema}

        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        with tempfile.TemporaryDirectory(prefix="dch_tab_") as temp_dir:
            temp_parquet_path = os.path.join(temp_dir, parquet_filename)
            pq.write_table(arrow_table, temp_parquet_path, compression="snappy")

            if minio_manager is not None:
                object_key = cls.build_object_key(category, parquet_filename)
                uploaded = minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=object_key,
                    file_path=temp_parquet_path,
                    content_type="application/vnd.apache.parquet",
                )
                if not uploaded:
                    raise HTTPException(status_code=500, detail="Failed to upload Parquet to MinIO.")

                created_minio_object = object_key
                accessibility_type = AccessibilityType.Minio
                accessibility_params = TabularAccessibilityParams(
                    bucket=bucket_name,
                    object_name=object_key,
                    row_count=row_count,
                    columns=columns_list,
                    compression="snappy",
                    schema_definition=schema_dict,
                )
            else:
                permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id))
                os.makedirs(permanent_dir, exist_ok=True)
                stored_file_path = os.path.join(permanent_dir, parquet_filename)

                shutil.copy2(temp_parquet_path, stored_file_path)
                created_local_file = stored_file_path
                accessibility_type = AccessibilityType.File
                accessibility_params = TabularAccessibilityParams(
                    file_path=stored_file_path,
                    path_base="media_rel",
                    row_count=row_count,
                    columns=columns_list,
                    compression="snappy",
                    schema_definition=schema_dict,
                )

            try:
                access = LayerAccessibility(
                    accessibility_type=accessibility_type.value,
                    params=accessibility_params.model_dump(mode="json"),
                )
                session.add(access)
                session.flush()

                layer_info = LayerInfo(
                    uuid=uuid_str,
                    app_label="dch",
                    title=title,
                    layer_name=os.path.splitext(parquet_filename)[0],
                    srid=0,
                    extent=[],
                    extent_3857=None,
                    data_model=DataModel.T.name,
                    model_type=model_type,
                    geom_type=[],
                    uploaded_by=uploaded_by,
                    temporal_res=temporal_date,
                    category_id=category.id,
                    layer_accessibility=access,
                    quality_level=None,
                    format="PARQUET",
                )
                session.add(layer_info)
                session.flush()

                return LayerIngestSummary(
                    title=title,
                    status="SUCCESS",
                    data_model=DataModel.T.name,
                    uuid=uuid_str,
                    layer_info_id=layer_info.id,
                    layer_access_id=access.id,
                    records_count=row_count,
                    format="PARQUET",
                )
            except Exception as e:
                if created_minio_object and minio_manager:
                    minio_manager.delete_object(bucket_name, created_minio_object)
                if created_local_file and os.path.exists(created_local_file):
                    os.remove(created_local_file)
                raise e

    # =========================================================================
    # C. DOCUMENT FILE PERSISTENCE HANDLER
    # =========================================================================

    @classmethod
    def process_document_file(
        cls,
        session: Session,
        *,
        title: str,
        category: LayerCategory,
        temporal_date: date,
        model_type: Optional[str],
        source_path: str,
        uploaded_by: Optional[int],
        minio_manager: Optional[MinioManager],
        bucket_name: str,
    ) -> LayerIngestSummary:
        """Persists unstructured documents (PDF, Excel, Word, XML) into the catalog.

        Inspects MIME type, records byte size, copies the file to the target storage
        destination (MinIO or local media root), and creates corresponding catalog records.

        Args:
            session: Active SQLAlchemy database session.
            title: Human-readable layer title.
            category: Resolved `LayerCategory` database model.
            temporal_date: Python date object representing temporal acquisition.
            model_type: Domain taxonomy tag.
            source_path: Local filesystem path to the target document.
            uploaded_by: Database ID of the user triggering ingestion.
            minio_manager: MinIO manager singleton instance (if MinIO is active).
            bucket_name: Target MinIO/S3 bucket name.

        Returns:
            LayerIngestSummary: Ingestion details including file size in bytes and file format.

        Raises:
            HTTPException: 404 Not Found if the document file does not exist.
            HTTPException: 500 Internal Server Error on storage or transaction failures.
        """
        if not os.path.exists(source_path):
            raise HTTPException(status_code=404, detail=f"Document file not found: {source_path}")

        uuid_str = DCHUtils.get_uuid1()
        original_name = os.path.basename(source_path)
        ext = os.path.splitext(original_name)[1].lower()
        safe_title = "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
        target_filename = f"{safe_title}_{uuid_str[:8]}{ext}"

        mime_type, _ = mimetypes.guess_type(source_path)
        mime_type = mime_type or "application/octet-stream"
        file_size = os.path.getsize(source_path)

        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        if minio_manager is not None:
            object_key = cls.build_object_key(category, target_filename)
            uploaded = minio_manager.upload_file(
                bucket_name=bucket_name,
                object_name=object_key,
                file_path=source_path,
                content_type=mime_type,
            )
            if not uploaded:
                raise HTTPException(status_code=500, detail="Failed to upload document to MinIO.")

            created_minio_object = object_key
            accessibility_type = AccessibilityType.Minio
            accessibility_params = DocumentAccessibilityParams(
                bucket=bucket_name,
                object_name=object_key,
                mime_type=mime_type,
                file_size_bytes=file_size,
                original_extension=ext,
            )
        else:
            permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id))
            os.makedirs(permanent_dir, exist_ok=True)
            stored_file_path = os.path.join(permanent_dir, target_filename)

            shutil.copy2(source_path, stored_file_path)
            created_local_file = stored_file_path
            accessibility_type = AccessibilityType.File
            accessibility_params = DocumentAccessibilityParams(
                file_path=stored_file_path,
                path_base="media_rel",
                mime_type=mime_type,
                file_size_bytes=file_size,
                original_extension=ext,
            )

        try:
            access = LayerAccessibility(
                accessibility_type=accessibility_type.value,
                params=accessibility_params.model_dump(mode="json"),
            )
            session.add(access)
            session.flush()

            layer_info = LayerInfo(
                uuid=uuid_str,
                app_label="dch",
                title=title,
                layer_name=os.path.splitext(target_filename)[0],
                srid=0,
                extent=[],
                extent_3857=None,
                data_model=DataModel.D.name,
                model_type=model_type,
                geom_type=[],
                uploaded_by=uploaded_by,
                temporal_res=temporal_date,
                category_id=category.id,
                layer_accessibility=access,
                quality_level=None,
                format=ext.replace(".", "").upper() or "DOC",
            )
            session.add(layer_info)
            session.flush()

            return LayerIngestSummary(
                title=title,
                status="SUCCESS",
                data_model=DataModel.D.name,
                uuid=uuid_str,
                layer_info_id=layer_info.id,
                layer_access_id=access.id,
                records_count=file_size,
                format=ext.replace(".", "").upper(),
            )
        except Exception as e:
            if created_minio_object and minio_manager:
                minio_manager.delete_object(bucket_name, created_minio_object)
            if created_local_file and os.path.exists(created_local_file):
                os.remove(created_local_file)
            raise e

    # =========================================================================
    # D. INGESTION ROUTING DISPATCHER
    # =========================================================================

    @classmethod
    def ingest_single_layer(
        cls,
        session: Session,
        layer_payload: PipelineLayerPayload,
        uploaded_by: Optional[int],
        minio_manager: Optional[MinioManager],
        bucket_name: str,
    ) -> LayerIngestSummary:
        """Polymorphically routes an individual layer payload to its dedicated handler.

        Dispatches to:
            - `RasterIngestService.ingest_from_path` for Raster assets.
            - `VectorIngestService.ingest_from_path` for Vector datasets.
            - `process_tabular_json` for in-memory or file-based tabular JSON datasets.
            - `process_document_file` for arbitrary binary documents.

        Args:
            session: Active SQLAlchemy database session.
            layer_payload: `PipelineLayerPayload` instance defining the layer to ingest.
            uploaded_by: Database ID of the user performing ingestion.
            minio_manager: MinIO manager singleton instance (if MinIO is active).
            bucket_name: Destination S3/MinIO bucket name.

        Returns:
            LayerIngestSummary: Execution outcome with generated IDs, UUID, and format.

        Raises:
            HTTPException: 400 Bad Request on invalid category or unsupported data model.
        """
        data_model = cls.resolve_data_model(
            source_path=layer_payload.source_path,
            inline_records=layer_payload.inline_records,
            explicit=getattr(layer_payload.data_model, "value", None) or layer_payload.data_model,
        )
        category_id = cls.resolve_category_id(session, layer_payload.category)
        category = session.get(LayerCategory, category_id)

        temporal_date = (
            datetime.strptime(layer_payload.temporal, "%Y-%m-%d").date()
            if layer_payload.temporal
            else date.today()
        )

        # 1. RASTER ASSETS
        if data_model == DataModel.R.name:
            r_res: RasterIngestResult = RasterIngestService.ingest_from_path(
                session=session,
                minio_manager=minio_manager,
                title=layer_payload.title,
                category_id=category_id,
                raster_path=layer_payload.source_path,
                temporal=layer_payload.temporal,
                model_type=layer_payload.model_type,
                uploaded_by=uploaded_by,
                bucket_name=bucket_name,
            )
            return LayerIngestSummary(
                title=layer_payload.title,
                status="SUCCESS",
                data_model=DataModel.R.name,
                uuid=r_res.uuid,
                layer_info_id=r_res.layer_info_id,
                layer_access_id=r_res.layer_access_id,
                format="COG",
            )

        # 2. VECTOR ASSETS (Resolved across PostGIS, FlatGeobuf, GeoParquet, GeoPackage)
        elif data_model == DataModel.V.name:
            # Engine Resolution Logic:
            # 1. Explicit vector_engine on payload
            # 2. If target == DATABASE -> POSTGIS
            # 3. If target == LAKEHOUSE -> FLATGEOBUF (or GEOPARQUET if indicated)
            if layer_payload.vector_engine:
                engine_type = layer_payload.vector_engine
            elif layer_payload.target == TargetPersistence.DATABASE:
                engine_type = VectorStorageEngineType.POSTGIS
            else:
                engine_type = VectorStorageEngineType.FLATGEOBUF

            v_res: VectorIngestResult = VectorIngestService.ingest_from_path(
                session=session,
                vector_path=layer_payload.source_path,
                title=layer_payload.title,
                category_id=category_id,
                engine_type=engine_type,
                temporal=layer_payload.temporal,
                model_type=layer_payload.model_type,
                uploaded_by=uploaded_by,
                symbology=layer_payload.metadata_extra.get("symbology") if layer_payload.metadata_extra else None,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
                extra_engine_params=layer_payload.metadata_extra,
            )
            return LayerIngestSummary(
                title=layer_payload.title,
                status="SUCCESS",
                data_model=DataModel.V.name,
                uuid=v_res.uuid,
                layer_info_id=v_res.layer_info_id,
                layer_access_id=v_res.layer_access_id,
                records_count=v_res.feature_count,
                format=v_res.format,
            )

        # 3. TABULAR / TELEMETRY JSON
        elif data_model == DataModel.T.name:
            return cls.process_tabular_json(
                session=session,
                title=layer_payload.title,
                category=category,
                temporal_date=temporal_date,
                model_type=layer_payload.model_type,
                source_path=layer_payload.source_path,
                inline_records=layer_payload.inline_records,
                uploaded_by=uploaded_by,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
            )

        # 4. UNSTRUCTURED DOCUMENTS
        elif data_model == DataModel.D.name:
            return cls.process_document_file(
                session=session,
                title=layer_payload.title,
                category=category,
                temporal_date=temporal_date,
                model_type=layer_payload.model_type,
                source_path=layer_payload.source_path,
                uploaded_by=uploaded_by,
                minio_manager=minio_manager,
                bucket_name=bucket_name,
            )

        raise HTTPException(status_code=400, detail=f"Unsupported spatial data model '{data_model}'.")

    # =========================================================================
    # E. BATCH EXECUTION RUNNER
    # =========================================================================

    @classmethod
    def execute_pipeline(
        cls,
        session: Session,
        request: PipelineBatchRequest,
        uploaded_by: Optional[int] = None,
        minio_manager: Optional[MinioManager] = None,
    ) -> PipelineBatchResult:
        """Executes a multi-layer batch pipeline with atomic or best-effort policies.

        Iterates across layers defined in the JSON payload, resolving dependencies,
        routing each item, and recording overall execution statistics.

        Args:
            session: Active SQLAlchemy database session.
            request: `PipelineBatchRequest` instance specifying batch run parameters.
            uploaded_by: Database ID of the user executing the pipeline.
            minio_manager: MinIO manager singleton instance (if MinIO is active).

        Returns:
            PipelineBatchResult: Summary report of requested, successful, and failed counts,
                accompanied by individual layer execution summaries.

        Raises:
            HTTPException: 500 Internal Server Error if an atomic pipeline run encounters any failure.
        """
        results: List[LayerIngestSummary] = []
        successful_count = 0
        failed_count = 0

        da_logger.info(f"Executing Batch Pipeline: '{request.pipeline_name}' | Total: {len(request.layers)}")

        if getattr(request, "atomic", False):
            with session.begin_nested():
                try:
                    for layer in request.layers:
                        summary = cls.ingest_single_layer(
                            session=session,
                            layer_payload=layer,
                            uploaded_by=uploaded_by,
                            minio_manager=minio_manager,
                            bucket_name=request.bucket_name,
                        )
                        results.append(summary)
                        successful_count += 1
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    da_logger.exception(f"Atomic pipeline failed: {exc}")
                    raise HTTPException(status_code=500, detail=f"Atomic execution failed: {str(exc)}")
        else:
            for layer in request.layers:
                try:
                    summary = cls.ingest_single_layer(
                        session=session,
                        layer_payload=layer,
                        uploaded_by=uploaded_by,
                        minio_manager=minio_manager,
                        bucket_name=request.bucket_name,
                    )
                    results.append(summary)
                    successful_count += 1
                except Exception as e:
                    session.rollback()
                    failed_count += 1
                    error_msg = str(getattr(e, "detail", e))
                    da_logger.warning(f"Batch item failed '{layer.title}': {error_msg}")
                    results.append(
                        LayerIngestSummary(
                            title=layer.title,
                            status="FAILED",
                            data_model=getattr(layer.data_model, "value", layer.data_model) or "UNKNOWN",
                            error_message=error_msg,
                        )
                    )

        return PipelineBatchResult(
            pipeline_name=request.pipeline_name,
            total_requested=len(request.layers),
            successful_count=successful_count,
            failed_count=failed_count,
            results=results,
        )