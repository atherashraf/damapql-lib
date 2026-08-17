import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.damapql.dch.dtypes import (LayerAccessibilityParams, MinioAccessibilityParams, FileAccessibilityParams, )
from app.damapql.dch.models import (LayerAccessibility, LayerCategory, LayerInfo, AccessibilityType, DataModel, )
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import (DCH_APP_NAME, MINIO_BUCKET_NAME, MEDIA_BASE, )
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.utils.singletons import da_logger


@dataclass
class RasterIngestResult:
    uuid: str
    layer_info_id: int
    layer_access_id: int


class RasterIngestService:

    # ============================================================
    # GENERAL HELPERS
    # ============================================================

    @staticmethod
    def build_object_key(category: LayerCategory, filename: str, ) -> str:
        """
        Construct MinIO object key/path.

        Example:
            dch/main_category/sub_category/file.tif
        """

        sub_cat = category.sub or "general"

        return (f"{DCH_APP_NAME.lower()}/"
                f"{category.main}/"
                f"{sub_cat}/"
                f"{filename}")

    @staticmethod
    def parse_temporal(temporal: str, ) -> date:
        try:
            return datetime.strptime(temporal, "%Y-%m-%d", ).date()

        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.", ) from e

    @staticmethod
    def build_unique_file_name(title: str, uuid_str: str, temporal: str, original_name: str, ) -> str:

        file_ext = os.path.splitext(original_name)[1]

        safe_title = (title.strip().replace("/", "_").replace("\\", "_").replace(" ", "_"))

        return (f"{safe_title}_"
                f"{uuid_str}_"
                f"{temporal}"
                f"{file_ext}")

    # ============================================================
    # COG
    # ============================================================

    @staticmethod
    def ensure_cog(local_src_path: str, ) -> str:
        """
        Ensure raster is COG-compatible.

        IMPORTANT:
        This assumes COGRaster.open_from_local() validates COG
        compatibility. If it only opens any GeoTIFF, replace this
        check with your explicit COG validation method.
        """

        file_ext = os.path.splitext(local_src_path)[1]

        try:
            raster = COGRaster.open_from_local(local_src_path)

            try:
                raster.close()
            except Exception:
                pass

            return local_src_path

        except Exception:

            cog_path = local_src_path.replace(file_ext, f"_cog{file_ext}", )

            cog_path = COGRaster.create_cog(local_src_path, des_path=cog_path, )

            try:
                os.remove(local_src_path)

            except Exception as e:
                da_logger.warning(f"Failed to delete original "
                                  f"temporary raster: {e}")

            return cog_path

    # ============================================================
    # ACCESSIBILITY PARAMS
    # ============================================================

    @staticmethod
    def build_minio_accessibility_params(*, bucket_name: str, object_name: str,
            local_raster_path: str, ) -> MinioAccessibilityParams:
        """
        Build MinIO accessibility parameters while extracting
        raster metadata from a locally accessible copy.
        """

        raster = RioRaster(local_raster_path)

        try:
            return MinioAccessibilityParams(bucket=bucket_name, object_name=object_name,
                spectral_resolution=(raster.get_spectral_resolution()),
                spatial_resolution=(raster.get_spatial_resolution()),
                radiometric_resolution=(raster.get_radiometric_resolution()), )

        finally:
            raster.close()

    @staticmethod
    def build_file_accessibility_params(local_path: str, ) -> FileAccessibilityParams:
        """
        Build file accessibility parameters.

        Files inside MEDIA_BASE are stored as media-relative.
        Files outside MEDIA_BASE are stored as absolute paths.
        """

        local_path = os.path.abspath(local_path)

        media_base_abs = os.path.abspath(MEDIA_BASE)

        try:
            common_path = os.path.commonpath([media_base_abs, local_path, ])

        except ValueError:
            common_path = None

        if common_path == media_base_abs:
            relative_path = os.path.relpath(local_path, media_base_abs, )

            return FileAccessibilityParams(file_path=relative_path, path_base="media_rel", )

        return FileAccessibilityParams(file_path=local_path, path_base="absolute", )

    # ============================================================
    # ACCESS RECORD
    # ============================================================

    @staticmethod
    def create_access_record(session: Session, accessibility_type: AccessibilityType,
            accessibility_params: LayerAccessibilityParams, ) -> LayerAccessibility:

        params = (accessibility_params.model_dump(mode="json") if isinstance(accessibility_params,
            BaseModel, ) else accessibility_params)

        access = LayerAccessibility(accessibility_type=accessibility_type.value, params=params, )

        session.add(access)
        session.flush()

        return access

    # ============================================================
    # LAYER INFO
    # ============================================================

    @staticmethod
    def create_layer_info(session: Session, *, uuid_str: str, title: str, temporal_date: date,
            data_model_type: str | None, access: LayerAccessibility, category: LayerCategory,
            uploaded_by: Optional[int], raster_metadata_path: str, ) -> LayerInfo:
        """
        Create LayerInfo using a locally accessible raster path.

        raster_metadata_path may be:
        - permanent local raster
        - temporary downloaded MinIO raster
        - temporary COG before MinIO upload
        """

        raster = RioRaster(raster_metadata_path)

        try:
            file_name = os.path.basename(raster_metadata_path)

            layer_name = os.path.splitext(file_name)[0]

            envelope = raster.get_envelop()

            layer_info = LayerInfo(uuid=uuid_str, app_label="dch", title=title, layer_name=layer_name,

                srid=raster.get_raster_srid(),

                extent=(envelope.total_bounds.tolist()),

                extent_3857=(envelope.to_crs(epsg=3857).total_bounds.tolist()),

                data_model=DataModel.R.name, model_type=data_model_type,

                geom_type=[],

                uploaded_by=uploaded_by, temporal_res=temporal_date,

                category_id=category.id, layer_access_id=access.id,

                quality_level=None, format="COG", )

            session.add(layer_info)
            session.flush()

            return layer_info

        finally:
            raster.close()

    # ============================================================
    # COMMON REGISTRATION
    # ============================================================

    @classmethod
    def _register_existing_raster(cls, session: Session, *, uuid_str: str, title: str, temporal_date: date,
            data_model_type: str | None, uploaded_by: Optional[int], category: LayerCategory,
            accessibility_type: AccessibilityType, accessibility_params: LayerAccessibilityParams,
            raster_metadata_path: str, ) -> LayerInfo:

        access = cls.create_access_record(session=session, accessibility_type=accessibility_type,
            accessibility_params=accessibility_params, )

        return cls.create_layer_info(session=session, uuid_str=uuid_str, title=title, temporal_date=temporal_date,
            data_model_type=data_model_type, access=access, category=category, uploaded_by=uploaded_by,
            raster_metadata_path=raster_metadata_path, )

    # ============================================================
    # UPLOAD NEW RASTER
    # ============================================================

    @classmethod
    def ingest_upload(cls, session: Session, minio_manager: Optional[MinioManager] = None, *, title: str,
            category_id: int, temporal: str, data_model_type: str, raster_file: UploadFile, uploaded_by: Optional[int],
            bucket_name: str = MINIO_BUCKET_NAME, ) -> RasterIngestResult:

        category = session.get(LayerCategory, category_id, )

        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID", )

        if not raster_file.filename:
            raise HTTPException(status_code=400, detail="Raster file name is missing.", )

        temporal_date = cls.parse_temporal(temporal)

        uuid_str = DCHUtils.get_uuid1()

        unique_name = cls.build_unique_file_name(title=title, uuid_str=uuid_str, temporal=temporal,
            original_name=raster_file.filename, )

        object_key = cls.build_object_key(category, unique_name, )

        is_minio_active = (minio_manager is not None)

        stored_file_path: str | None = None
        minio_uploaded = False

        with tempfile.TemporaryDirectory() as tmp_dir:

            temp_local_path = os.path.join(tmp_dir, unique_name, )

            # ----------------------------------------------------
            # Save uploaded file to temporary location
            # ----------------------------------------------------

            with open(temp_local_path, "wb", ) as f:

                shutil.copyfileobj(raster_file.file, f, )

            # ----------------------------------------------------
            # Convert / ensure COG
            # ----------------------------------------------------

            cog_local_path = cls.ensure_cog(temp_local_path)

            try:

                # ==================================================
                # MINIO
                # ==================================================

                if is_minio_active:

                    minio_uploaded = (minio_manager.upload_file(bucket_name=bucket_name, object_name=object_key,
                        file_path=cog_local_path, content_type="image/tiff", ))

                    if not minio_uploaded:
                        raise HTTPException(status_code=500, detail=("Failed to upload raster "
                                                                     "to MinIO."), )

                    accessibility_type = (AccessibilityType.Minio)

                    accessibility_params = (
                        cls.build_minio_accessibility_params(bucket_name=bucket_name, object_name=object_key,
                            local_raster_path=cog_local_path, ))

                    raster_metadata_path = (cog_local_path)

                    da_logger.info("Raster uploaded to "
                                   f"s3://{bucket_name}/{object_key}")

                # ==================================================
                # LOCAL
                # ==================================================

                else:

                    permanent_dir = os.path.join(MEDIA_BASE, category.slug or str(category.id), )

                    os.makedirs(permanent_dir, exist_ok=True, )

                    cog_filename = os.path.basename(cog_local_path)

                    stored_file_path = os.path.join(permanent_dir, cog_filename, )

                    shutil.copy2(cog_local_path, stored_file_path, )

                    accessibility_type = (AccessibilityType.File)

                    accessibility_params = (cls.build_file_accessibility_params(stored_file_path))

                    raster_metadata_path = (stored_file_path)

                    da_logger.info("Raster stored locally at "
                                   f"{stored_file_path}")

                # ==================================================
                # DATABASE
                # ==================================================

                layer_info = cls._register_existing_raster(session=session, uuid_str=uuid_str, title=title,
                    temporal_date=temporal_date, data_model_type=data_model_type, uploaded_by=uploaded_by,
                    category=category, accessibility_type=accessibility_type, accessibility_params=accessibility_params,
                    raster_metadata_path=raster_metadata_path, )

                session.commit()

                return RasterIngestResult(uuid=layer_info.uuid, layer_info_id=layer_info.id,
                    layer_access_id=(layer_info.layer_access_id), )

            except Exception as e:

                da_logger.exception(f"Failed to ingest raster: {e}")

                session.rollback()

                # ------------------------------------------------
                # Rollback MinIO object
                # ------------------------------------------------

                if (minio_uploaded and minio_manager):
                    try:
                        minio_manager.delete_object(bucket_name, object_key, )

                    except Exception as rollback_error:
                        da_logger.warning("Failed to rollback MinIO object "
                                          f"{object_key}: {rollback_error}")

                # ------------------------------------------------
                # Rollback local file
                # ------------------------------------------------

                if (stored_file_path and os.path.exists(stored_file_path)):
                    try:
                        os.remove(stored_file_path)

                    except Exception as rollback_error:
                        da_logger.warning("Failed to rollback local raster "
                                          f"{stored_file_path}: "
                                          f"{rollback_error}")

                raise

    # ============================================================
    # REGISTER EXISTING RASTER
    # ============================================================

    @classmethod
    def ingest_from_path(cls, session: Session, minio_manager: Optional[MinioManager] = None, *, title: str,
            category_id: int, raster_path: str, temporal: str | None, data_model_type: str | None,
            uploaded_by: Optional[int], bucket_name: str = MINIO_BUCKET_NAME, ) -> RasterIngestResult:
        """Register an existing raster without uploading/copying it.

        If minio_manager exists:
            raster_path = MinIO object name/key (or s3://bucket/key or bucket/key)
        Otherwise:
            raster_path = local filesystem path
        """
        category = session.get(LayerCategory, category_id)

        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID", )

        if not raster_path:
            raise HTTPException(status_code=400, detail="Raster path is required.", )

        uuid_str = DCHUtils.get_uuid1()
        temporal_date = cls.parse_temporal(temporal) if temporal else date.today()
        is_minio_active = minio_manager is not None

        try:
            # ==================================================
            # EXISTING MINIO OBJECT
            # ==================================================
            if is_minio_active:
                # 1. Clean protocols and slashes
                cleaned = raster_path.strip()
                for prefix in ("s3://", "minio://"):
                    if cleaned.startswith(prefix):
                        cleaned = cleaned[len(prefix):]

                cleaned = cleaned.strip("/")

                # 2. Strip bucket_name if present as the root folder
                bucket_prefix = f"{bucket_name}/"
                if cleaned.startswith(bucket_prefix):
                    object_name = cleaned[len(bucket_prefix):]
                else:
                    object_name = cleaned

                if not object_name:
                    raise HTTPException(status_code=400, detail="Invalid MinIO object key.", )

                # 3. Check existence
                if not minio_manager.object_exists(bucket_name, object_name):
                    raise HTTPException(status_code=404,
                        detail=(f"Raster not found in MinIO: s3://{bucket_name}/{object_name}"), )

                # ------------------------------------------------
                # Temporary local copy only for metadata
                # ------------------------------------------------
                with tempfile.TemporaryDirectory() as tmp_dir:
                    file_name = (os.path.basename(object_name) or f"{uuid_str}.tif")
                    local_path = os.path.join(tmp_dir, file_name)

                    downloaded = minio_manager.download_file(bucket_name=bucket_name, object_name=object_name,
                        destination_path=local_path, )

                    if not downloaded:
                        raise HTTPException(status_code=500,
                            detail="Failed to temporarily download raster from MinIO.", )

                    accessibility_params = cls.build_minio_accessibility_params(bucket_name=bucket_name,
                        object_name=object_name, local_raster_path=local_path, )

                    layer_info = cls._register_existing_raster(session=session, uuid_str=uuid_str, title=title,
                        temporal_date=temporal_date, data_model_type=data_model_type, uploaded_by=uploaded_by,
                        category=category, accessibility_type=AccessibilityType.Minio,
                        accessibility_params=accessibility_params, raster_metadata_path=local_path, )

                da_logger.info(f"Existing MinIO raster registered: s3://{bucket_name}/{object_name}")

            # ==================================================
            # EXISTING LOCAL FILE
            # ==================================================
            else:
                local_path = os.path.abspath(raster_path)

                if not os.path.isfile(local_path):
                    raise HTTPException(status_code=404, detail=f"Raster file not found: {local_path}", )

                accessibility_params = cls.build_file_accessibility_params(local_path)

                layer_info = cls._register_existing_raster(session=session, uuid_str=uuid_str, title=title,
                    temporal_date=temporal_date, data_model_type=data_model_type, uploaded_by=uploaded_by,
                    category=category, accessibility_type=AccessibilityType.File,
                    accessibility_params=accessibility_params, raster_metadata_path=local_path, )

                da_logger.info(f"Existing local raster registered: {local_path}")

            # ==================================================
            # COMMIT
            # ==================================================
            session.commit()

            return RasterIngestResult(uuid=layer_info.uuid, layer_info_id=layer_info.id,
                layer_access_id=layer_info.layer_access_id, )

        except HTTPException:
            session.rollback()
            raise

        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to register existing raster '{raster_path}': {e}")
            raise
