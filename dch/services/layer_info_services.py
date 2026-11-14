import os
import uuid
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple

from digitalarzengine.utils.singletons import da_logger
from sqlalchemy import select


from app.damapql.dch.models import (
    LayerInfo,
    LayerAccessibility,
    LayerCategory,
    DBConnection,
    AccessibilityType,
    DataModel,
    ModelType,
)

from app.damapql.dch.utils.dch_utils import DCHUtils
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.rio_raster import RioRaster

from app.damapql.dch.utils.model_inspector import ModelInspector
from app.damapql.dch.utils.spatial_analyzer import SpatialAnalyzer
from settings import BASE_DIR, DEFAULT_EXTENT, MEDIA_DIR


class LayerInfoService:
    def __init__(
        self,
        model_inspector: Optional[ModelInspector] = None,
        spatial_analyzer: Optional[SpatialAnalyzer] = None,
    ):
        self.model_inspector = model_inspector or ModelInspector()
        self.spatial_analyzer = spatial_analyzer or SpatialAnalyzer(DEFAULT_EXTENT)

    # ------------------------------------------------------------------
    # generic helpers
    # ------------------------------------------------------------------

    def _get_enum_name(self, enum_cls, *preferred_names, fallback=None):
        for name in preferred_names:
            if hasattr(enum_cls, name):
                return getattr(enum_cls, name).name

        if fallback and hasattr(enum_cls, fallback):
            return getattr(enum_cls, fallback).name

        for attr in dir(enum_cls):
            if attr.startswith("_"):
                continue
            val = getattr(enum_cls, attr)
            if hasattr(val, "name"):
                return val.name
        return None

    def _resolve_vector_data_model(self) -> Optional[str]:
        return self._get_enum_name(DataModel, "V", "VECTOR", fallback="V")

    def _resolve_raster_data_model(self) -> Optional[str]:
        return self._get_enum_name(DataModel, "R", "RASTER", fallback="R")

    def _resolve_vector_model_type(self) -> Optional[str]:
        return self._get_enum_name(ModelType, "Geom", "Geometry", "FEATURE", fallback="V")

    def _resolve_table_model_type(self) -> Optional[str]:
        return self._get_enum_name(ModelType, "T", "TABLE", fallback="T")

    def _resolve_raster_model_type(self, explicit_value: Optional[str] = None) -> Optional[str]:
        if explicit_value:
            return explicit_value
        return self._get_enum_name(ModelType, "R", "RASTER", "TILE", fallback="R")

    def _resolve_path(self, file_path: str) -> str:
        p = Path(file_path)
        if p.is_absolute():
            return str(p)
        return str(Path(BASE_DIR) / file_path)

    def _get_existing_layer_by_app_and_name(self, db, app_label: str, layer_name: str):
        qs = select(LayerInfo).where(
            LayerInfo.app_label == app_label,
            LayerInfo.layer_name == layer_name,
        )
        result = db.execute(qs)
        return result.scalar_one_or_none()

    def _get_username_or_id(self, user) -> Optional[str]:
        if user is None:
            return None
        return (
            getattr(user, "username", None)
            or getattr(user, "email", None)
            or str(getattr(user, "id", None))
        )

    # ------------------------------------------------------------------
    # accessibility helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _create_or_update_db_accessibility(
            *,
            table_name: str,
            db_conn_name: str,
            columns: dict,
            pk_cols: list,
    ):
        return LayerAccessibility.create_db_accessibility(
            table_name=table_name,
            db_conn=db_conn_name,
            columns=columns,
            pk_cols=pk_cols,
        )

    @staticmethod
    def _create_raster_access_record(
        *,
        db,
        cog_path: str,
    ):
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
        db.add(access)
        db.flush()
        return access

    # ------------------------------------------------------------------
    # metadata builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_vector_extra_metadata(
        *,
        model_class,
        geom_col_name: Optional[str],
        primary_col_name: Optional[str],
        pk_cols: List[str],
        columns: Dict[str, str],
    ) -> dict:
        return {
            "source_model": model_class.__name__,
            "source_module": model_class.__module__,
            "auto_created": True,
            "geom_column": geom_col_name,
            "primary_column": primary_col_name,
            "pk_cols": pk_cols,
            "columns": columns,
        }

    @staticmethod
    def _build_raster_extra_metadata(
        *,
        raster_path: str,
        cog_path: str,
        temporal: Optional[str],
        data_model_type: Optional[str],
        raster,
    ) -> dict:
        return {
            "raster_path": raster_path,
            "cog_path": cog_path,
            "relative_path": os.path.relpath(cog_path, MEDIA_DIR),
            "temporal": temporal,
            "data_model_type": data_model_type,
            "srid": raster.get_raster_srid(),
            "spectral_resolution": raster.get_spectral_resolution(),
            "spatial_resolution": raster.get_spatial_resolution(),
            "radiometric_resolution": str(raster.get_radiometric_resolution()),
            "auto_created": True,
        }

    # ------------------------------------------------------------------
    # vector methods
    # ------------------------------------------------------------------

    @staticmethod
    def format_model_name_to_title(name: str) -> str:
        """
        Converts CamelCase or snake_case to human readable title.
        Keeps 2-letter words and acronyms uppercase (e.g., NA, PP, WASA).
        """
        import re

        def fix(word: str) -> str:
            # keep acronyms (2+ uppercase letters) as-is
            if word.isupper():
                return word

            # force 2-letter words to uppercase (NA, PP, UC)
            if len(word) == 2:
                return word.upper()

            return word.capitalize()

        # snake_case → words
        if "_" in name:
            parts = [p for p in name.split("_") if p]
            return " ".join(fix(p) for p in parts)

        # CamelCase → split words
        parts = re.findall(
            r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+',
            name
        )

        return " ".join(fix(p) for p in parts if p)

    def save_model_layer_info(
            self,
            *,
            db,
            app_label: str,
            model_class,
            layer_category_id,
            quality_level_id=None,
            user=None,
            engine=None,
            db_conn_name: Optional[str] = None,
            overwrite: bool = True,
            include_non_spatial: bool = False,
            layer_title: Optional[str] = None,
            layer_name: Optional[str] = None,
    ):
        if engine is None:
            engine = self.model_inspector.get_model_engine(app_label)

        if db_conn_name is None:
            db_conn_name = self.model_inspector.get_db_connection_name_for_app(
                app_label=app_label,
                db_connection_model=DBConnection,
            )

        table_name = layer_name or self.model_inspector.get_table_name(model_class)

        if not self.model_inspector.is_table_present(engine, table_name):
            da_logger.warning(f"Table not found for model {model_class.__name__}: {table_name}")
            return None, False

        geom_col_name = self.model_inspector.get_geometry_column_name(model_class)
        if not geom_col_name and not include_non_spatial:
            da_logger.warning(f"Skipping non-spatial model {model_class.__name__}")
            return None, False

        pk_cols = self.model_inspector.get_pk_cols(model_class)
        primary_col_name = self.model_inspector.get_primary_column_name(model_class)
        columns = self.model_inspector.get_non_geom_columns(model_class, geom_col_name)

        layer_access =  LayerAccessibility.create_db_accessibility(
            table_name=table_name,
            db_conn=db_conn_name,
            columns=columns,
            pk_cols=pk_cols,
        )

        if layer_access not in db:
            db.add(layer_access)
        db.flush()

        spatial_meta = self.spatial_analyzer.build_spatial_metadata(
            engine=engine,
            table_name=table_name,
            geom_col_name=geom_col_name,
        )

        existing_layer = self._get_existing_layer_by_app_and_name(
            db=db,
            app_label=app_label,
            layer_name=table_name,
        )

        has_geometry = bool(geom_col_name)
        data_model = self._resolve_vector_data_model()
        model_type = (
            self._resolve_vector_model_type()
            if has_geometry else self._resolve_table_model_type()
        )

        # final_title = layer_title or model_class.__name__
        final_title = layer_title or self.format_model_name_to_title(model_class.__name__)
        da_logger.info(f"final title {final_title}")
        actor = self._get_username_or_id(user)

        extra_metadata = self._build_vector_extra_metadata(
            model_class=model_class,
            geom_col_name=geom_col_name,
            primary_col_name=primary_col_name,
            pk_cols=pk_cols,
            columns=columns,
        )

        if existing_layer is None:
            obj = LayerInfo(
                uuid=str(uuid.uuid4()),
                app_label=app_label,
                title=final_title,
                layer_name=table_name,
                srid=spatial_meta["srid"],
                proj4=None,
                extent=spatial_meta["extent"],
                extent_3857=spatial_meta["extent_3857"],
                data_model=data_model,
                model_type=model_type,
                geom_type=spatial_meta["geom_type"],
                zoom_range=[0, 30],
                layer_access_id=layer_access.id,
                category_id=layer_category_id,
                quality_level_id=quality_level_id,
                format="MVT",
                uploaded_by=actor,
                extra_metadata=extra_metadata,
            )
            obj.save()
            return obj, True

        if not overwrite:
            da_logger.info(f"LayerInfo already exists for {app_label}.{table_name}")
            return existing_layer, False

        existing_layer.title = final_title
        existing_layer.srid = spatial_meta["srid"]
        existing_layer.extent = spatial_meta["extent"]
        existing_layer.extent_3857 = spatial_meta["extent_3857"]
        existing_layer.data_model = data_model
        existing_layer.model_type = model_type
        existing_layer.geom_type = spatial_meta["geom_type"]
        existing_layer.layer_access_id = layer_access.id
        existing_layer.category_id = layer_category_id
        existing_layer.format = "MVT"
        existing_layer.extra_metadata = {
            **(existing_layer.extra_metadata or {}),
            **extra_metadata,
        }
        existing_layer.save()
        return existing_layer, True

    def save_app_layer_infos(
            self,
            *,
            db,
            app_label: str,
            layer_category_id: int,
            quality_level_id: Optional[int] = None,
            user: Any = None,
            overwrite: bool = True,
            include_non_spatial: bool = False,
            target_model: Optional[Any] = None,  # Accept direct model class
            model_name: Optional[str] = None,  # Accept string model name
            table_name: Optional[str] = None,  # Accept string table name
    ) -> dict:
        model_classes = self.model_inspector.get_model_classes_from_app(app_label)

        # 1. Filter by direct model class
        if target_model is not None:
            model_classes = [m for m in model_classes if m == target_model]

        # 2. Filter by string model name
        elif model_name:
            target_name = model_name.strip().lower()
            model_classes = [
                m for m in model_classes if m.__name__.lower() == target_name
            ]

        # 3. Filter by string table name
        elif table_name:
            target_table = table_name.strip().lower()
            model_classes = [
                m for m in model_classes
                if self.model_inspector.get_table_name(m).lower() == target_table
            ]

        if not model_classes:
            return {
                "created_or_updated": 0,
                "skipped": 0,
                "failed": 0,
                "messages": [
                    f"No matching models found in app '{app_label}' "
                    f"(target_model='{target_model}', model_name='{model_name}', table_name='{table_name}')"
                ],
            }

        engine = self.model_inspector.get_model_engine(app_label)
        db_conn_name = self.model_inspector.get_db_connection_name_for_app(
            app_label=app_label,
            db_connection_model=DBConnection,
        )

        result = {
            "created_or_updated": 0,
            "skipped": 0,
            "failed": 0,
            "messages": [],
        }

        for model_class in model_classes:
            try:
                _, changed = self.save_model_layer_info(
                    db=db,
                    app_label=app_label,
                    model_class=model_class,
                    layer_category_id=layer_category_id,
                    quality_level_id=quality_level_id,
                    user=user,
                    engine=engine,
                    db_conn_name=db_conn_name,
                    overwrite=overwrite,
                    include_non_spatial=include_non_spatial,
                )

                if changed:
                    result["created_or_updated"] += 1
                    result["messages"].append(f"saved: {model_class.__name__}")
                else:
                    result["skipped"] += 1
                    result["messages"].append(f"skipped: {model_class.__name__}")

            except Exception as e:
                da_logger.exception(f"Failed for model {model_class.__name__}: {e}")
                result["failed"] += 1
                result["messages"].append(f"failed: {model_class.__name__} -> {str(e)}")

        return result

    # ------------------------------------------------------------------
    # raster methods
    # ------------------------------------------------------------------

    def ensure_cog(self, src_path: str) -> str:
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

    def _sanitize_extent(self, extent):
        if self.spatial_analyzer._is_valid_extent(extent):
            return [float(v) for v in extent]
        return list(DEFAULT_EXTENT)

    def _read_raster_info(self, cog_path: str):
        raster = RioRaster(cog_path)
        envelope = raster.get_envelop()

        extent = envelope.total_bounds.tolist()
        extent_3857 = envelope.to_crs(epsg=3857).total_bounds.tolist()

        extent = self._sanitize_extent(extent)
        extent_3857 = self._sanitize_extent(extent_3857)

        return {
            "raster": raster,
            "srid": raster.get_raster_srid(),
            "extent": extent,
            "extent_3857": extent_3857,
        }

    def save_raster_layer_info(
        self,
        *,
        db,
        title: str,
        raster_path: str,
        layer_category_id: int,
        temporal: Optional[str] = None,
        data_model_type: Optional[str] = None,
        user=None,
        overwrite: bool = False,
        app_label: str = "dch",
        layer_name: Optional[str] = None,
    ) -> Tuple[LayerInfo, bool]:
        """
        Register raster from full path or BASE_DIR-relative path.
        Ensures raster is COG before creating LayerInfo.
        """
        resolved_path = self._resolve_path(raster_path)

        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"Raster file not found: {resolved_path}")

        ext = Path(resolved_path).suffix.lower()
        if ext not in {".tif", ".tiff"}:
            raise ValueError("Only .tif / .tiff raster files are supported")

        category = db.get(LayerCategory, layer_category_id)
        if not category:
            raise ValueError(f"Invalid category id: {layer_category_id}")

        final_cog_path = self.ensure_cog(resolved_path)
        raster_info = self._read_raster_info(final_cog_path)
        raster = raster_info["raster"]

        file_name = os.path.basename(final_cog_path)
        final_layer_name = layer_name or os.path.splitext(file_name)[0]

        existing_layer = self._get_existing_layer_by_app_and_name(
            db=db,
            app_label=app_label,
            layer_name=final_layer_name,
        )

        if existing_layer is not None and not overwrite:
            return existing_layer, False

        access = self._create_raster_access_record(
            db=db,
            cog_path=final_cog_path,
        )

        extra_metadata = self._build_raster_extra_metadata(
            raster_path=resolved_path,
            cog_path=final_cog_path,
            temporal=temporal,
            data_model_type=data_model_type,
            raster=raster,
        )

        actor = self._get_username_or_id(user)

        if existing_layer is None:
            obj = LayerInfo(
                uuid=str(uuid.uuid4()),
                app_label=app_label,
                title=title,
                layer_name=final_layer_name,
                srid=raster_info["srid"],
                extent=raster_info["extent"],
                extent_3857=raster_info["extent_3857"],
                data_model=self._resolve_raster_data_model(),
                model_type=self._resolve_raster_model_type(data_model_type),
                geom_type=[],
                uploaded_by=actor,
                temporal_res=temporal,
                category_id=category.id,
                layer_access_id=access.id,
                quality_level=None,
                format="COG",
                extra_metadata=extra_metadata,
            )
            db.add(obj)
            db.flush()
            return obj, True

        existing_layer.title = title
        existing_layer.srid = raster_info["srid"]
        existing_layer.extent = raster_info["extent"]
        existing_layer.extent_3857 = raster_info["extent_3857"]
        existing_layer.data_model = self._resolve_raster_data_model()
        existing_layer.model_type = self._resolve_raster_model_type(data_model_type)
        existing_layer.uploaded_by = actor
        existing_layer.temporal_res = temporal
        existing_layer.category_id = category.id
        existing_layer.layer_access_id = access.id
        existing_layer.format = "COG"
        existing_layer.extra_metadata = {
            **(existing_layer.extra_metadata or {}),
            **extra_metadata,
        }
        db.flush()
        return existing_layer, True

    @staticmethod
    def build_layer_object_name(
            *,
            file_name: str,
            model_type: str,
            uuid_str: str,
            db=None,
            layer_category_id: Optional[int] = None,
            category: Optional[LayerCategory] = None,
            ext: str | None = None,
    ) -> str:
        if category is None:
            if db is None or layer_category_id is None:
                raise ValueError("Either 'category' or ('db' and 'layer_category_id') must be provided")
            category = db.get(LayerCategory, layer_category_id)
            if not category:
                raise ValueError(f"Invalid category id: {layer_category_id}")

        if getattr(category, "main", None):
            category_main = category.main
            category_sub = getattr(category, "sub", None)
        elif getattr(category, "parent", None):
            category_main = category.parent.name
            category_sub = category.name
        else:
            category_main = getattr(category, "main_category", None) or getattr(category, "name", "general")
            category_sub = getattr(category, "sub_category", None)

        return DCHUtils.build_versioned_object_name(
            file_name=file_name,
            category_main=category_main,
            category_sub=category_sub,
            model_type=model_type,
            uuid_str=uuid_str,
            ext=ext,
        )