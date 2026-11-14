"""Pipeline Ingestion Service Module.

Provides unified gateway orchestration for declarative, JSON-driven data ingestion
into the Data Catalog Hub (DCH). Ingests spatial, tabular, and unstructured document assets
with multi-target persistence routing.
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
from typing import Any, Dict, List, Literal, Optional, Union

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.api_enum import DataModel, ModelType
from app.damapql.dch.models import (
    AccessibilityType,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
)
from app.damapql.dch.services.ingestion.raster_ingest_service import (
    RasterIngestResult,
    RasterIngestService,
)
from app.damapql.dch.services.ingestion.vector import (
    VectorIngestResult,
    VectorIngestService,
    VectorStorageEngineType,
)
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import DCH_DATA_DB_KEY, MEDIA_BASE, MINIO_BUCKET_NAME
from app.damapql.utils.adapters.deps import get_sync_engine_instance


# =============================================================================
# 1. ENUMS & PYDANTIC SPECIFICATIONS
# =============================================================================

class TargetPersistence(str, Enum):
    """Storage persistence target mode."""
    DATABASE = "database"
    LAKEHOUSE = "lakehouse"


class CategorySpec(BaseModel):
    """Declarative specification for catalog taxonomy categorization."""
    id: Optional[int] = None
    main: Optional[str] = None
    sub: Optional[str] = "general"


class PipelineLayerPayload(BaseModel):
    """Declarative specification for an individual spatial, tabular, or document layer."""
    title: str = Field(..., min_length=2, max_length=255, description="Layer title.")
    category: CategorySpec = Field(..., description="Target category specification.")
    source_path: Optional[str] = Field(None, description="Filesystem path or URI to the input file.")
    inline_records: Optional[Union[List[Dict[str, Any]], Dict[str, Any]]] = Field(
        None, description="Raw JSON array or dictionary to convert to Parquet."
    )
    data_model: Optional[DataModel] = Field(
        None, description="Data model identifier ('R', 'V', 'T', 'D'). Inferred if omitted."
    )
    temporal: Optional[str] = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO 8601 temporal acquisition date (YYYY-MM-DD)."
    )
    model_type: Optional[str] = Field(None, description="Domain taxonomy classification tag.")
    target: TargetPersistence = Field(
        default=TargetPersistence.LAKEHOUSE, description="Storage persistence routing destination."
    )
    vector_engine: Optional[VectorStorageEngineType] = Field(
        default=None, description="Direct vector engine override (postgis, flatgeobuf, geoparquet, geopackage)."
    )
    metadata_extra: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Supplemental metadata attributes."
    )

    @field_validator("data_model", mode="before")
    @classmethod
    def normalize_data_model(cls, value: Any) -> Optional[DataModel]:
        if value is None or isinstance(value, DataModel):
            return value
        if isinstance(value, str):
            cleaned = value.strip()
            for member in DataModel:
                if cleaned.upper() == member.name.upper() or cleaned.lower() == member.value.lower():
                    return member
        raise ValueError(f"Invalid data_model '{value}'. Expected one of: {[m.name for m in DataModel]}.")

    @model_validator(mode="after")
    def validate_source_presence(self) -> PipelineLayerPayload:
        if not self.source_path and self.inline_records is None:
            raise ValueError("Either 'source_path' or 'inline_records' must be supplied.")
        return self


class PipelineBatchRequest(BaseModel):
    """Root JSON specification payload for running batch pipeline ingestion."""
    pipeline_name: str = Field(..., description="Identifier for this pipeline run.")
    atomic: bool = Field(
        default=False, description="Whether to enforce atomic transaction boundaries across all layers."
    )
    bucket_name: str = Field(default=MINIO_BUCKET_NAME, description="Destination object storage bucket name.")
    layers: List[PipelineLayerPayload] = Field(
        ..., min_length=1, description="Collection of layer specifications to ingest."
    )


# =============================================================================
# 2. ACCESSIBILITY PARAMS & DTOs
# =============================================================================

class TabularAccessibilityParams(BaseModel):
    """Metadata parameters describing Parquet-converted tabular and telemetry datasets."""
    bucket: Optional[str] = None
    object_name: Optional[str] = None
    file_path: Optional[str] = None
    path_base: Optional[str] = None
    row_count: int
    columns: List[str]
    compression: str = "snappy"
    schema_definition: Dict[str, str] = Field(default_factory=dict)


class DocumentAccessibilityParams(BaseModel):
    """Metadata parameters describing unstructured document files."""
    bucket: Optional[str] = None
    object_name: Optional[str] = None
    file_path: Optional[str] = None
    path_base: Optional[str] = None
    mime_type: str
    file_size_bytes: int
    original_extension: str


@dataclass
class _CreatedArtifact:
    """Tracks external side effects (MinIO objects, local files, PostGIS tables) for atomic batch rollback."""
    minio_object: Optional[str] = None
    local_file: Optional[str] = None
    postgis_table: Optional[str] = None
    postgis_schema: str = "public"
    postgis_db_key: str = DCH_DATA_DB_KEY


@dataclass
class LayerIngestSummary:
    """Ingestion execution summary for an individual layer item."""
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
    """Aggregated outcome of an entire batch pipeline run."""
    pipeline_name: str
    total_requested: int
    successful_count: int
    failed_count: int
    results: List[LayerIngestSummary] = field(default_factory=list)


# =============================================================================
# 3. PIPELINE INGESTION SERVICE
# =============================================================================

class PipelineIngestService:
    """Unified Ingestion Gateway for Raster, Vector, Tabular (JSON to Parquet), and Document assets."""

    RASTER_EXT = {".tif", ".tiff", ".vrt"}
    VECTOR_EXT = {".zip", ".shp", ".gpkg", ".geojson", ".fgb", ".geoparquet"}
    TABULAR_EXT = {".json", ".jsonl", ".csv", ".parquet"}
    DOCUMENT_EXT = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".txt", ".md", ".xml", ".tex", ".latex"}

    DOC_EXT_MODEL_TYPE: Dict[str, str] = {
        ".pdf": ModelType.PDF.name,
        ".doc": ModelType.DOC.name,
        ".docx": ModelType.DOC.name,
        ".txt": ModelType.Txt.name,
        ".md": ModelType.Txt.name,
        ".tex": ModelType.Latex.name,
        ".latex": ModelType.Latex.name,
    }

    # =========================================================================
    # A. RESOLUTION & CLASSIFICATION HELPERS
    # =========================================================================

    @classmethod
    def resolve_data_model(
        cls,
        source_path: Optional[str],
        inline_records: Optional[Union[List[Any], Dict[str, Any]]],
        explicit: Optional[Union[DataModel, str]] = None,
    ) -> str:
        if explicit is not None:
            if isinstance(explicit, DataModel):
                return explicit.name
            cleaned = str(explicit).strip()
            for member in DataModel:
                if cleaned.upper() == member.name.upper() or cleaned.lower() == member.value.lower():
                    return member.name
            return cleaned.upper()

        if inline_records is not None:
            return DataModel.T.name

        if not source_path:
            raise HTTPException(status_code=400, detail="Unable to infer data model without source.")

        ext = os.path.splitext(source_path.strip().lower())[1]
        if ext in cls.RASTER_EXT:
            return DataModel.R.name
        if ext in cls.VECTOR_EXT:
            return DataModel.V.name
        if ext in cls.TABULAR_EXT:
            return DataModel.T.name
        if ext in cls.DOCUMENT_EXT:
            return DataModel.D.name

        return DataModel.D.name

    @staticmethod
    def resolve_category_id(session: Session, category_spec: Any) -> int:
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
    def build_object_key(
        *,
        category: LayerCategory,
        title: str,
        ext: str,
        model_type: str,
        uuid_str: str,
    ) -> str:
        """Builds an immutable versioned relative path matching Raster & Vector pipelines."""
        clean_ext = ext if ext.startswith(".") else f".{ext}"
        return LayerInfoService.build_layer_object_name(
            category=category,
            file_name=f"{title}{clean_ext}",
            model_type=model_type,
            uuid_str=uuid_str,
        )

    @staticmethod
    def _cleanup_artifacts(
        artifacts: List[_CreatedArtifact],
        minio_manager: Optional[MinioManager],
        bucket_name: str,
    ) -> None:
        """Rolls back MinIO objects, local files, and PostGIS tables on atomic batch failure."""
        for art in reversed(artifacts):
            if art.minio_object and minio_manager:
                try:
                    minio_manager.delete_object(bucket_name, art.minio_object)
                    da_logger.info(f"Atomic rollback: removed MinIO object {art.minio_object}")
                except Exception as err:
                    da_logger.warning(f"Atomic rollback failed for MinIO object {art.minio_object}: {err}")

            if art.local_file and os.path.exists(art.local_file):
                try:
                    os.remove(art.local_file)
                    da_logger.info(f"Atomic rollback: removed local file {art.local_file}")
                except Exception as err:
                    da_logger.warning(f"Atomic rollback failed for local file {art.local_file}: {err}")

            if art.postgis_table:
                try:
                    db_engine = get_sync_engine_instance(key=art.postgis_db_key)
                    with db_engine.begin() as conn:
                        conn.execute(
                            text(f'DROP TABLE IF EXISTS "{art.postgis_schema}"."{art.postgis_table}" CASCADE;')
                        )
                    da_logger.info(
                        f"Atomic rollback: dropped PostGIS table {art.postgis_schema}.{art.postgis_table}"
                    )
                except Exception as err:
                    da_logger.warning(f"Atomic rollback failed for PostGIS table {art.postgis_table}: {err}")

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
        artifacts_out: Optional[List[_CreatedArtifact]] = None,
    ) -> LayerIngestSummary:
        uuid_str = DCHUtils.get_uuid1()
        resolved_model_type = model_type or ModelType.Table.name

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

        relative_key = cls.build_object_key(
            category=category,
            title=title,
            ext=".parquet",
            model_type=resolved_model_type,
            uuid_str=uuid_str,
        )
        layer_name = os.path.splitext(os.path.basename(relative_key))[0]

        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        with tempfile.TemporaryDirectory(prefix="dch_tab_") as temp_dir:
            temp_parquet_path = os.path.join(temp_dir, f"{layer_name}.parquet")
            pq.write_table(arrow_table, temp_parquet_path, compression="snappy")

            if minio_manager is not None:
                uploaded = minio_manager.upload_file(
                    bucket_name=bucket_name,
                    object_name=relative_key,
                    file_path=temp_parquet_path,
                    content_type="application/vnd.apache.parquet",
                )
                if not uploaded:
                    raise HTTPException(status_code=500, detail="Failed to upload Parquet to MinIO.")

                created_minio_object = relative_key
                accessibility_type = AccessibilityType.Minio
                accessibility_params = TabularAccessibilityParams(
                    bucket=bucket_name,
                    object_name=relative_key,
                    row_count=row_count,
                    columns=columns_list,
                    compression="snappy",
                    schema_definition=schema_dict,
                )
            else:
                stored_file_path = os.path.join(MEDIA_BASE, *relative_key.split("/"))
                os.makedirs(os.path.dirname(stored_file_path), exist_ok=True)
                shutil.copy2(temp_parquet_path, stored_file_path)

                created_local_file = stored_file_path
                accessibility_type = AccessibilityType.File
                accessibility_params = TabularAccessibilityParams(
                    file_path=relative_key,
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
                    title=title.strip(),
                    layer_name=layer_name,
                    srid=0,
                    extent=[],
                    extent_3857=None,
                    data_model=DataModel.T.name,
                    model_type=resolved_model_type,
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

                if artifacts_out is not None:
                    artifacts_out.append(
                        _CreatedArtifact(
                            minio_object=created_minio_object,
                            local_file=created_local_file,
                        )
                    )

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
            except Exception:
                if created_minio_object and minio_manager:
                    minio_manager.delete_object(bucket_name, created_minio_object)
                if created_local_file and os.path.exists(created_local_file):
                    os.remove(created_local_file)
                raise

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
        artifacts_out: Optional[List[_CreatedArtifact]] = None,
    ) -> LayerIngestSummary:
        if not os.path.exists(source_path):
            raise HTTPException(status_code=404, detail=f"Document file not found: {source_path}")

        uuid_str = DCHUtils.get_uuid1()
        original_name = os.path.basename(source_path)
        ext = os.path.splitext(original_name)[1].lower()
        resolved_model_type = model_type or cls.DOC_EXT_MODEL_TYPE.get(ext, ModelType.Other.name)

        mime_type, _ = mimetypes.guess_type(source_path)
        mime_type = mime_type or "application/octet-stream"
        file_size = os.path.getsize(source_path)

        relative_key = cls.build_object_key(
            category=category,
            title=title,
            ext=ext or ".bin",
            model_type=resolved_model_type,
            uuid_str=uuid_str,
        )
        layer_name = os.path.splitext(os.path.basename(relative_key))[0]

        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        if minio_manager is not None:
            uploaded = minio_manager.upload_file(
                bucket_name=bucket_name,
                object_name=relative_key,
                file_path=source_path,
                content_type=mime_type,
            )
            if not uploaded:
                raise HTTPException(status_code=500, detail="Failed to upload document to MinIO.")

            created_minio_object = relative_key
            accessibility_type = AccessibilityType.Minio
            accessibility_params = DocumentAccessibilityParams(
                bucket=bucket_name,
                object_name=relative_key,
                mime_type=mime_type,
                file_size_bytes=file_size,
                original_extension=ext,
            )
        else:
            stored_file_path = os.path.join(MEDIA_BASE, *relative_key.split("/"))
            os.makedirs(os.path.dirname(stored_file_path), exist_ok=True)
            shutil.copy2(source_path, stored_file_path)

            created_local_file = stored_file_path
            accessibility_type = AccessibilityType.File
            accessibility_params = DocumentAccessibilityParams(
                file_path=relative_key,
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
                title=title.strip(),
                layer_name=layer_name,
                srid=0,
                extent=[],
                extent_3857=None,
                data_model=DataModel.D.name,
                model_type=resolved_model_type,
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

            if artifacts_out is not None:
                artifacts_out.append(
                    _CreatedArtifact(
                        minio_object=created_minio_object,
                        local_file=created_local_file,
                    )
                )

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
        except Exception:
            if created_minio_object and minio_manager:
                minio_manager.delete_object(bucket_name, created_minio_object)
            if created_local_file and os.path.exists(created_local_file):
                os.remove(created_local_file)
            raise

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
        artifacts_out: Optional[List[_CreatedArtifact]] = None,
    ) -> LayerIngestSummary:
        data_model = cls.resolve_data_model(
            source_path=layer_payload.source_path,
            inline_records=layer_payload.inline_records,
            explicit=layer_payload.data_model,
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
            if artifacts_out is not None:
                access = session.get(LayerAccessibility, r_res.layer_access_id)
                params = (access.params or {}) if access else {}
                artifacts_out.append(
                    _CreatedArtifact(
                        minio_object=params.get("object_name"),
                        local_file=(
                            os.path.join(MEDIA_BASE, *params["file_path"].split("/"))
                            if params.get("file_path") and params.get("path_base") == "media_rel"
                            else params.get("file_path")
                        ),
                    )
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

        # 2. VECTOR ASSETS (PostGIS, FlatGeobuf, GeoParquet, GeoPackage)
        if data_model == DataModel.V.name:
            if layer_payload.vector_engine:
                engine_type = layer_payload.vector_engine
            elif layer_payload.target == TargetPersistence.DATABASE:
                engine_type = VectorStorageEngineType.POSTGIS
            else:
                engine_type = VectorStorageEngineType.GEOPARQUET

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
            if artifacts_out is not None:
                access = session.get(LayerAccessibility, v_res.layer_access_id)
                params = (access.params or {}) if access else {}
                extra = layer_payload.metadata_extra or {}
                artifacts_out.append(
                    _CreatedArtifact(
                        minio_object=params.get("object_name"),
                        local_file=(
                            os.path.join(MEDIA_BASE, *params["file_path"].split("/"))
                            if params.get("file_path") and params.get("path_base") == "media_rel"
                            else params.get("file_path")
                        ),
                        postgis_table=v_res.table_name,
                        postgis_schema=extra.get("schema_name", "public"),
                        postgis_db_key=extra.get("db_key") or DCH_DATA_DB_KEY,
                    )
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
        if data_model == DataModel.T.name:
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
                artifacts_out=artifacts_out,
            )

        # 4. UNSTRUCTURED DOCUMENTS
        if data_model == DataModel.D.name:
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
                artifacts_out=artifacts_out,
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
        results: List[LayerIngestSummary] = []
        successful_count = 0
        failed_count = 0

        da_logger.info(f"Executing Batch Pipeline: '{request.pipeline_name}' | Total: {len(request.layers)}")

        if getattr(request, "atomic", False):
            created_artifacts: List[_CreatedArtifact] = []
            try:
                for layer in request.layers:
                    summary = cls.ingest_single_layer(
                        session=session,
                        layer_payload=layer,
                        uploaded_by=uploaded_by,
                        minio_manager=minio_manager,
                        bucket_name=request.bucket_name,
                        artifacts_out=created_artifacts,
                    )
                    results.append(summary)
                    successful_count += 1
                session.commit()
            except Exception as exc:
                session.rollback()
                cls._cleanup_artifacts(created_artifacts, minio_manager, request.bucket_name)
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
                            data_model=layer.data_model.name if isinstance(layer.data_model, DataModel) else "UNKNOWN",
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