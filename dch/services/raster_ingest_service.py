import os
import shutil
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.damapql.dch.models import (
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
    AccessibilityType,
    DataModel,
)
from app.damapql.dch.utils.dch_utils import DCHUtils
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.rio_raster import RioRaster
from app.damapql.settings import APP_NAME, MEDIA_DIR



@dataclass
class RasterIngestResult:
    uuid: str
    raster_path: str
    cog_path: str
    relative_path: str
    layer_info_id: int
    layer_access_id: int


class RasterIngestService:
    @staticmethod
    def build_storage_folder(category: LayerCategory) -> str:
        folder_path = os.path.join(
            MEDIA_DIR,
            APP_NAME,
            category.main,
            category.sub or "general",
        )
        os.makedirs(folder_path, exist_ok=True)
        return folder_path

    @staticmethod
    def parse_temporal(temporal: str) -> date:
        try:
            return datetime.strptime(temporal, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.")

    @staticmethod
    def build_unique_file_name(title: str, uuid_str: str, temporal: str, original_name: str) -> str:
        file_ext = os.path.splitext(original_name)[1]
        safe_title = title.strip().replace("/", "_").replace("\\", "_").replace(" ", "_")
        return f"{safe_title}_{uuid_str}_{temporal}{file_ext}"

    @staticmethod
    def save_upload(upload: UploadFile, dst_path: str) -> None:
        with open(dst_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)

    @staticmethod
    def ensure_cog(src_path: str) -> str:
        """
        If already openable as COG, keep it.
        Otherwise create a COG next to the source and remove the original.
        """
        file_ext = os.path.splitext(src_path)[1]

        try:
            _ = COGRaster.open_from_local(src_path)
            return src_path
        except Exception:
            cog_path = src_path.replace(file_ext, f"_cog{file_ext}")
            cog_path = COGRaster.create_cog(src_path, des_path=cog_path)

            try:
                os.remove(src_path)
            except Exception as e:
                da_logger.warning(f"Failed to delete original raster after COG conversion: {e}")

            return cog_path

    @staticmethod
    def create_access_record(session: Session, cog_path: str) -> LayerAccessibility:
        raster = RioRaster(cog_path)
        relative_path = os.path.relpath(cog_path, MEDIA_DIR)
        rad_res = raster.get_radiometric_resolution()

        access = LayerAccessibility(
            accessibility_type=AccessibilityType.File.name,
            params={
                "file_path": relative_path,
                "is_path_media_rel": True,
                "spectral_resolution": raster.get_spectral_resolution(),
                "spatial_resolution": raster.get_spatial_resolution(),
                "radiometric_resolution": f"{rad_res}",
            },
        )
        session.add(access)
        session.flush()
        return access

    @staticmethod
    def create_layer_info(
        session: Session,
        *,
        uuid_str: str,
        title: str,
        temporal_date: date,
        data_model_type: str,
        access: LayerAccessibility,
        category: LayerCategory,
        uploaded_by: Optional[int],
        cog_path: str,
    ) -> LayerInfo:
        raster = RioRaster(cog_path)
        file_name = os.path.basename(cog_path)
        layer_name = os.path.splitext(file_name)[0]
        envelope = raster.get_envelop()

        layer_info = LayerInfo(
            uuid=uuid_str,
            app_label="dch",
            title=title,
            layer_name=layer_name,
            srid=raster.get_raster_srid(),
            extent=envelope.total_bounds.tolist(),
            extent_3857=envelope.to_crs(epsg=3857).total_bounds.tolist(),
            data_model=DataModel.R.name,
            model_type=data_model_type,
            geom_type=[],
            uploaded_by=uploaded_by,
            temporal_res=temporal_date,
            category_id=category.id,
            layer_access_id=access.id,
            quality_level=None,
            format="COG",
        )
        session.add(layer_info)
        session.flush()
        return layer_info

    @classmethod
    def ingest_upload(
        cls,
        session: Session,
        *,
        title: str,
        category_id: int,
        temporal: str,
        data_model_type: str,
        raster_file: UploadFile,
        uploaded_by: Optional[int],
    ) -> RasterIngestResult:
        category = session.get(LayerCategory, category_id)
        if not category:
            raise HTTPException(status_code=400, detail="Invalid category ID")

        temporal_date = cls.parse_temporal(temporal)
        uuid_str = DCHUtils.get_uuid1()

        folder_path = cls.build_storage_folder(category)
        unique_name = cls.build_unique_file_name(title, uuid_str, temporal, raster_file.filename)
        raster_path = os.path.join(folder_path, unique_name)

        cls.save_upload(raster_file, raster_path)
        cog_path = raster_path

        try:
            cog_path = cls.ensure_cog(raster_path)
            access = cls.create_access_record(session, cog_path)
            layer_info = cls.create_layer_info(
                session,
                uuid_str=uuid_str,
                title=title,
                temporal_date=temporal_date,
                data_model_type=data_model_type,
                access=access,
                category=category,
                uploaded_by=uploaded_by,
                cog_path=cog_path,
            )
            session.commit()

            return RasterIngestResult(
                uuid=layer_info.uuid,
                raster_path=raster_path,
                cog_path=cog_path,
                relative_path=os.path.relpath(cog_path, MEDIA_DIR),
                layer_info_id=layer_info.id,
                layer_access_id=access.id,
            )

        except Exception:
            session.rollback()

            for path in {raster_path, cog_path}:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception as cleanup_err:
                    da_logger.warning(f"Raster cleanup failed for {path}: {cleanup_err}")

            raise