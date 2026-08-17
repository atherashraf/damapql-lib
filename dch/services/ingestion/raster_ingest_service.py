from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.damapql.dch.dtypes import (FileAccessibilityParams, LayerAccessibilityParams, MinioAccessibilityParams, )
from app.damapql.dch.models import (AccessibilityType, DataModel, LayerAccessibility, LayerCategory, LayerInfo, )
from app.damapql.dch.services.ingestion.staging_storage import (LocalStagingStorage, StagedFile, StagingStorage, )
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import (DCH_APP_NAME, MEDIA_BASE, MINIO_BUCKET_NAME, )
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


# =============================================================================
# 1. DATA TRANSFER OBJECTS (DTOs)
# =============================================================================

@dataclass
class RasterIngestResult:
    uuid: str
    layer_info_id: int
    layer_access_id: int


# =============================================================================
# 2. RASTER INGESTION SERVICE
# =============================================================================

class RasterIngestService:
    """Orchestrates Raster ingestion, COG validation/conversion, storage persistence,

    and database registration for both new uploads and existing file assets.
    """

    # =========================================================================
    # A. PATH RESOLUTION & NAMING HELPERS
    # =========================================================================

    @staticmethod
    def parse_temporal(temporal: str) -> date:
        """Parses an ISO date string into a Python date object."""
        try:
            return datetime.strptime(temporal, "%Y-%m-%d").date()
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.", ) from e

    @staticmethod
    def build_unique_file_name(title: str, uuid_str: str, temporal: str, original_name: str) -> str:
        """Constructs a deterministic, filesystem-safe unique file name."""
        file_ext = os.path.splitext(original_name)[1]
        safe_title = (title.strip().replace("/", "_").replace("\\", "_").replace(" ", "_"))
        return f"{safe_title}_{uuid_str}_{temporal}{file_ext}"

    @staticmethod
    def build_object_key(category: LayerCategory, filename: str) -> str:
        """Constructs a standardized MinIO object storage key: app/main/sub/filename."""
        sub_cat = category.sub or "general"
        return f"{DCH_APP_NAME.lower()}/{category.main}/{sub_cat}/{filename}"

    @staticmethod
    def normalize_minio_object_name(raster_path: str, bucket_name: str) -> str:
        """Normalizes and validates raw S3/MinIO paths and URI prefixes."""
        cleaned = raster_path.strip()
        explicit_uri = False

        for prefix in ("s3://", "minio://"):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
                explicit_uri = True
                break

        cleaned = cleaned.strip("/")
        if not cleaned:
            raise HTTPException(status_code=400, detail="Invalid MinIO object path.")

        if explicit_uri:
            parts = cleaned.split("/", 1)
            uri_bucket = parts[0]
            if uri_bucket != bucket_name:
                raise HTTPException(status_code=400,
                                    detail=f"Raster belongs to bucket '{uri_bucket}', but expected '{bucket_name}'.", )
            if len(parts) != 2 or not parts[1]:
                raise HTTPException(status_code=400, detail="MinIO object key is missing.")
            return parts[1]

        bucket_prefix = f"{bucket_name}/"
        if cleaned.startswith(bucket_prefix):
            cleaned = cleaned[len(bucket_prefix):]

        if not cleaned:
            raise HTTPException(status_code=400, detail="Invalid MinIO object key.")

        return cleaned

    # =========================================================================
    # B. VALIDATION, COG CONVERSION & ROLLBACK HELPERS
    # =========================================================================

    @staticmethod
    def validate_raster(raster: COGRaster) -> None:
        """Validates spatial reference integrity on the target raster."""
        if raster.get_crs() is None:
            raise HTTPException(status_code=400, detail=("Raster CRS is missing. A georeferenced raster with a "
                                                         "valid coordinate reference system is required."), )
        if raster.get_raster_srid() == 0:
            da_logger.warning("Raster CRS exists but could not be resolved to a standard EPSG code.")

    @staticmethod
    def ensure_cog(local_src_path: str) -> str:
        """Verifies if the file at local path is a COG; converts and removes source if not."""
        if COGRaster.is_cog(local_src_path):
            return local_src_path

        base_name = os.path.splitext(local_src_path)[0]
        cog_path = f"{base_name}_cog.tif"
        cog_path = COGRaster.create_cog(local_src_path, des_path=cog_path)

        try:
            os.remove(local_src_path)
        except Exception as e:
            da_logger.warning(f"Failed to delete original temporary raster: {e}")

        return cog_path

    @staticmethod
    def _rollback_created_raster(*, minio_manager: Optional[MinioManager], bucket_name: str,
                                 created_minio_object: Optional[str], created_local_file: Optional[str], ) -> None:
        """Rollback helper to remove newly written artifacts if a transaction aborts."""
        if created_minio_object and minio_manager:
            try:
                minio_manager.delete_object(bucket_name, created_minio_object)
            except Exception as e:
                da_logger.warning(f"Failed to rollback generated MinIO COG {created_minio_object}: {e}")

        if created_local_file and os.path.exists(created_local_file):
            try:
                os.remove(created_local_file)
            except Exception as e:
                da_logger.warning(f"Failed to rollback generated local COG {created_local_file}: {e}")

    # =========================================================================
    # C. METADATA & DATABASE REGISTRATION
    # =========================================================================

    @staticmethod
    def build_minio_accessibility_params(*, bucket_name: str, object_name: str,
                                         raster: COGRaster) -> MinioAccessibilityParams:
        """Constructs MinIO accessibility metadata parameters from COGRaster properties."""
        return MinioAccessibilityParams(bucket=bucket_name, object_name=object_name,
                                        spectral_resolution=raster.get_spectral_resolution(),
                                        spatial_resolution=raster.get_spatial_resolution(),
                                        radiometric_resolution=raster.get_radiometric_resolution(), )

    @staticmethod
    def build_file_accessibility_params(local_path: str, ) -> FileAccessibilityParams:
        """Constructs file accessibility metadata, checking relativity to MEDIA_BASE."""
        local_path = os.path.abspath(local_path)
        media_base_abs = os.path.abspath(MEDIA_BASE)

        try:
            common_path = os.path.commonpath([media_base_abs, local_path])
        except ValueError:
            common_path = None

        if common_path == media_base_abs:
            relative_path = os.path.relpath(local_path, media_base_abs)
            return FileAccessibilityParams(file_path=relative_path, path_base="media_rel")

        return FileAccessibilityParams(file_path=local_path, path_base="absolute")

    @staticmethod
    def create_access_record(session: Session, accessibility_type: AccessibilityType,
                             accessibility_params: LayerAccessibilityParams, ) -> LayerAccessibility:
        """Persists LayerAccessibility records in the database."""
        params = (accessibility_params.model_dump(mode="json") if isinstance(accessibility_params,
                                                                             BaseModel) else accessibility_params)
        access = LayerAccessibility(accessibility_type=accessibility_type.value, params=params, )
        session.add(access)
        session.flush()
        return access

    @staticmethod
    def create_layer_info(session: Session, *, uuid_str: str, title: str, layer_name: str, temporal_date: date,
                          model_type: Optional[str], access: LayerAccessibility, category: LayerCategory,
                          uploaded_by: Optional[int], raster: COGRaster, ) -> LayerInfo:
        """Persists spatial catalog LayerInfo metadata records in the database."""
        envelope = raster.get_envelop()
        layer_info = LayerInfo(uuid=uuid_str, app_label="dch", title=title, layer_name=layer_name,
                               srid=raster.get_raster_srid(), extent=envelope.total_bounds.tolist(),
                               extent_3857=envelope.to_crs(epsg=3857).total_bounds.tolist(),
                               data_model=DataModel.R.name, model_type=model_type, geom_type=[],
                               uploaded_by=uploaded_by, temporal_res=temporal_date, category_id=category.id,
                               layer_access_id=access.id, quality_level=None, format="COG", )
        session.add(layer_info)
        session.flush()
        return layer_info

    @classmethod
    def _register_existing_raster(cls, session: Session, *, uuid_str: str, title: str, layer_name: str,
                                  temporal_date: date, model_type: Optional[str], uploaded_by: Optional[int],
                                  category: LayerCategory, accessibility_type: AccessibilityType,
                                  accessibility_params: LayerAccessibilityParams, raster: COGRaster, ) -> LayerInfo:
        """Combined helper to register accessibility and LayerInfo in a single step."""
        access = cls.create_access_record(session=session, accessibility_type=accessibility_type,
                                          accessibility_params=accessibility_params, )
        return cls.create_layer_info(session=session, uuid_str=uuid_str, title=title, layer_name=layer_name,
                                     temporal_date=temporal_date, model_type=model_type, access=access,
                                     category=category, uploaded_by=uploaded_by, raster=raster, )

    # =========================================================================
    # D. INGESTION PIPELINE: UPLOAD FILE
    # =========================================================================

    @classmethod
    def ingest_upload(cls, session: Session, minio_manager: Optional[MinioManager] = None,
                      staging_storage: Optional[StagingStorage] = None, *, title: str, category_id: int, temporal: str,
                      model_type: str, raster_file: UploadFile, uploaded_by: Optional[int],
                      bucket_name: str = MINIO_BUCKET_NAME, ) -> RasterIngestResult:
        """Ingests a multipart uploaded raster, converts to COG, persists, and registers."""
        category = session.get(LayerCategory, category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID")
        if not raster_file.filename:
            raise HTTPException(status_code=400, detail="Raster file name is missing.")

        temporal_date = cls.parse_temporal(temporal)
        uuid_str = DCHUtils.get_uuid1()
        unique_name = cls.build_unique_file_name(title=title, uuid_str=uuid_str, temporal=temporal,
                                                 original_name=raster_file.filename, )

        is_minio_active = minio_manager is not None
        staging_storage = staging_storage or LocalStagingStorage()

        staged_file: Optional[StagedFile] = None
        stored_file_path: Optional[str] = None
        minio_uploaded = False
        object_key: Optional[str] = None
        persistent_raster: Optional[COGRaster] = None

        try:
            # 1. Staging
            staged_file = staging_storage.stage_upload(upload_file=raster_file, filename=unique_name)
            da_logger.info(f"Raster staged: {staged_file.local_path}")

            # 2. Convert / Ensure COG
            cog_local_path = cls.ensure_cog(staged_file.local_path)
            persistent_filename = os.path.basename(cog_local_path)

            # 3. Persistent Storage Routing
            if is_minio_active:
                object_key = cls.build_object_key(category, persistent_filename)
                minio_uploaded = minio_manager.upload_file(bucket_name=bucket_name, object_name=object_key,
                                                           file_path=cog_local_path, content_type="image/tiff", )
                if not minio_uploaded:
                    raise HTTPException(status_code=500, detail="Failed to upload raster to MinIO.", )

                accessibility_type = AccessibilityType.Minio
                basic_params = MinioAccessibilityParams(bucket=bucket_name, object_name=object_key)
                persistent_raster = COGRaster.open_from_minio(params=basic_params, minio_manager=minio_manager)

                if persistent_raster is None:
                    raise HTTPException(status_code=500,
                                        detail="Raster uploaded to MinIO, but persistent COG could not be opened.", )

                if not persistent_raster.is_valid_cog():
                    raise HTTPException(status_code=500, detail="Persistent MinIO raster is not a valid COG.", )

                accessibility_params = cls.build_minio_accessibility_params(bucket_name=bucket_name,
                                                                            object_name=object_key,
                                                                            raster=persistent_raster, )
                layer_name = os.path.splitext(persistent_filename)[0]
                da_logger.info(f"Raster stored in MinIO: s3://{bucket_name}/{object_key}")

            else:
                permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id))
                os.makedirs(permanent_dir, exist_ok=True)
                stored_file_path = os.path.join(permanent_dir, persistent_filename)

                shutil.copy2(cog_local_path, stored_file_path)
                accessibility_type = AccessibilityType.File
                accessibility_params = cls.build_file_accessibility_params(stored_file_path)

                persistent_raster = COGRaster.open_from_local(stored_file_path)
                if not persistent_raster.is_valid_cog():
                    raise HTTPException(status_code=500, detail="Persistent local raster is not a valid COG.", )

                layer_name = os.path.splitext(persistent_filename)[0]
                da_logger.info(f"Raster stored locally: {stored_file_path}")

            # 4. Metadata Validation & DB Registration
            cls.validate_raster(persistent_raster)
            layer_info = cls._register_existing_raster(session=session, uuid_str=uuid_str, title=title,
                                                       layer_name=layer_name, temporal_date=temporal_date,
                                                       model_type=model_type, uploaded_by=uploaded_by,
                                                       category=category, accessibility_type=accessibility_type,
                                                       accessibility_params=accessibility_params,
                                                       raster=persistent_raster, )

            session.commit()
            da_logger.info(f"Raster ingestion completed: {uuid_str}")

            return RasterIngestResult(uuid=layer_info.uuid, layer_info_id=layer_info.id,
                                      layer_access_id=layer_info.layer_access_id, )

        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to ingest raster: {e}")

            if minio_uploaded and minio_manager and object_key:
                try:
                    minio_manager.delete_object(bucket_name, object_key)
                except Exception as rollback_error:
                    da_logger.warning(f"Failed to rollback MinIO object {object_key}: {rollback_error}")

            if stored_file_path and os.path.exists(stored_file_path):
                try:
                    os.remove(stored_file_path)
                except Exception as rollback_error:
                    da_logger.warning(f"Failed to rollback local raster {stored_file_path}: {rollback_error}")
            raise

        finally:
            if staged_file:
                try:
                    staging_storage.cleanup(staged_file)
                except Exception as cleanup_error:
                    da_logger.warning(f"Failed to cleanup staging file {staged_file.local_path}: {cleanup_error}")

    # =========================================================================
    # E. INGESTION PIPELINE: REGISTER EXISTING PATH / OBJECT
    # =========================================================================

    @classmethod
    def ingest_from_path(cls, session: Session, minio_manager: Optional[MinioManager] = None, *, title: str,
            category_id: int, raster_path: str, temporal: Optional[str], model_type: Optional[str],
            uploaded_by: Optional[int], bucket_name: str = MINIO_BUCKET_NAME, ) -> RasterIngestResult:
        """
        Ingests an existing local raster path.

        If MinIO is available:
            local path -> COG -> MinIO -> register MinIO accessibility

        Otherwise:
            local path -> COG -> MEDIA_BASE -> register file accessibility
        """

        category = session.get(LayerCategory, category_id)

        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID", )

        if not raster_path:
            raise HTTPException(status_code=400, detail="Raster path is required.", )

        local_path = os.path.abspath(raster_path)

        if not os.path.isfile(local_path):
            raise HTTPException(status_code=404, detail=f"Raster file not found: {local_path}", )

        uuid_str = DCHUtils.get_uuid1()

        temporal_date = (cls.parse_temporal(temporal) if temporal else date.today())

        is_minio_active = minio_manager is not None

        created_minio_object: Optional[str] = None
        created_local_file: Optional[str] = None

        temp_dir: Optional[tempfile.TemporaryDirectory] = None

        try:
            # =========================================================
            # 1. OPEN SOURCE RASTER
            # =========================================================
            source_raster = COGRaster.open_from_local(local_path)

            if source_raster is None:
                raise HTTPException(status_code=500, detail=f"Unable to open raster: {local_path}", )

            source_filename = os.path.basename(local_path)
            source_stem = os.path.splitext(source_filename)[0]

            # =========================================================
            # 2. ENSURE COG
            # =========================================================
            if source_raster.is_valid_cog():
                cog_path = local_path
                persistent_filename = source_filename

            else:
                da_logger.info(f"Existing raster is not a COG. "
                               f"Converting: {local_path}")

                temp_dir = tempfile.TemporaryDirectory(prefix="digitalarz_cog_")

                persistent_filename = f"{source_stem}_cog.tif"

                cog_path = os.path.join(temp_dir.name, persistent_filename, )

                COGRaster.create_cog(local_path, des_path=cog_path, )

                converted_raster = COGRaster.open_from_local(cog_path)

                if (converted_raster is None or not converted_raster.is_valid_cog()):
                    raise HTTPException(status_code=500, detail="Generated raster is not a valid COG.", )

            # =========================================================
            # 3A. MINIO STORAGE
            # =========================================================
            if is_minio_active:
                object_name = cls.build_object_key(category=category, filename=persistent_filename, )

                da_logger.info(f"Uploading raster to MinIO: "
                               f"{local_path} -> "
                               f"s3://{bucket_name}/{object_name}")

                uploaded = minio_manager.upload_file(bucket_name=bucket_name, object_name=object_name,
                    file_path=cog_path, content_type="image/tiff", )

                if not uploaded:
                    raise HTTPException(status_code=500, detail="Failed to upload raster to MinIO.", )

                created_minio_object = object_name

                # -----------------------------------------------------
                # Re-open persistent raster FROM MinIO
                # -----------------------------------------------------
                basic_params = MinioAccessibilityParams(bucket=bucket_name, object_name=object_name, )

                raster = COGRaster.open_from_minio(params=basic_params, minio_manager=minio_manager, )

                if raster is None:
                    raise HTTPException(status_code=500, detail=("Raster uploaded to MinIO "
                                                                 "but could not be opened."), )

                if not raster.is_valid_cog():
                    raise HTTPException(status_code=500, detail=("Persistent MinIO raster "
                                                                 "is not a valid COG."), )

                accessibility_type = AccessibilityType.Minio

                accessibility_params = (
                    cls.build_minio_accessibility_params(bucket_name=bucket_name, object_name=object_name,
                        raster=raster, ))

                layer_name = os.path.splitext(persistent_filename)[0]

                da_logger.info(f"Raster stored in MinIO: "
                               f"s3://{bucket_name}/{object_name}")

            # =========================================================
            # 3B. LOCAL MEDIA STORAGE
            # =========================================================
            else:
                permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id), )

                os.makedirs(permanent_dir, exist_ok=True, )

                stored_file_path = os.path.join(permanent_dir, persistent_filename, )

                # Avoid copying file onto itself
                if os.path.abspath(cog_path) != os.path.abspath(stored_file_path):
                    shutil.copy2(cog_path, stored_file_path, )

                created_local_file = stored_file_path

                raster = COGRaster.open_from_local(stored_file_path)

                if raster is None:
                    raise HTTPException(status_code=500, detail=("Stored local raster "
                                                                 "could not be opened."), )

                if not raster.is_valid_cog():
                    raise HTTPException(status_code=500, detail=("Persistent local raster "
                                                                 "is not a valid COG."), )

                accessibility_type = AccessibilityType.File

                accessibility_params = (cls.build_file_accessibility_params(stored_file_path))

                layer_name = os.path.splitext(persistent_filename)[0]

                da_logger.info(f"Raster stored locally: "
                               f"{stored_file_path}")

            # =========================================================
            # 4. VALIDATE METADATA
            # =========================================================
            cls.validate_raster(raster)

            # =========================================================
            # 5. DATABASE REGISTRATION
            # =========================================================
            layer_info = cls._register_existing_raster(session=session, uuid_str=uuid_str, title=title,
                layer_name=layer_name, temporal_date=temporal_date, model_type=model_type,
                uploaded_by=uploaded_by, category=category, accessibility_type=accessibility_type,
                accessibility_params=accessibility_params, raster=raster, )

            session.commit()

            da_logger.info(f"Existing raster ingestion completed: {uuid_str}")

            return RasterIngestResult(uuid=layer_info.uuid, layer_info_id=layer_info.id,
                layer_access_id=layer_info.layer_access_id, )

        except Exception as exc:
            session.rollback()

            cls._rollback_created_raster(minio_manager=minio_manager, bucket_name=bucket_name,
                created_minio_object=created_minio_object, created_local_file=created_local_file, )

            if isinstance(exc, HTTPException):
                raise

            da_logger.exception(f"Failed to ingest existing raster "
                                f"'{raster_path}': {exc}")

            raise

        finally:
            if temp_dir is not None:
                try:
                    temp_dir.cleanup()
                except Exception as cleanup_error:
                    da_logger.warning(f"Failed to cleanup temporary COG: "
                                      f"{cleanup_error}")
