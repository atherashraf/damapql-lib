import importlib
import inspect
import pkgutil
from sqlalchemy import inspect as sql_inspect, Column
from typing import List, Type, Optional, Dict
from digitalarzengine.utils.singletons import da_logger

from app.damapql.utils.adapters.db import DBBase
from settings import DA_APPS


class ModelInspector:
    def __init__(self, da_apps=None):
        self.da_apps = da_apps or DA_APPS

    def get_app_config(self, app_label: str) -> dict:
        ap = self.da_apps.get(app_label)
        da_logger.debug(f"App info {ap}")
        if not ap:
            raise ValueError(f"App label '{app_label}' not found in DA_APPS.")
        return ap


    def is_sqlalchemy_model(self, obj) -> bool:
        return (
                inspect.isclass(obj)
                and hasattr(obj, "__tablename__")
                and hasattr(obj, "__table__")
                and hasattr(obj, "__mapper__")
                and not getattr(obj, "__abstract__", False)
        )
    def _collect_models_from_module(self, module) -> list:
        model_classes = []

        for _, obj in vars(module).items():
            if self.is_sqlalchemy_model(obj):
                model_classes.append(obj)
        da_logger.debug(f"Model classes {model_classes}")
        return model_classes
    def get_model_engine(self, app_label: str):
        ap = self.get_app_config(app_label)

        if "base" in ap:
            from app.damapql.manage import import_string
            BaseCls = import_string(ap["base"])
            return BaseCls.get_engine(db_key=ap["db_key"])

        return DBBase.get_engine(db_key=ap["db_key"])

    def get_table_name(self, model_class) -> str:
        return model_class.__tablename__

    def _collect_models_from_all(self, module) -> list:
        model_classes = []
        exported = getattr(module, "__all__", None)

        if not exported:
            return model_classes

        for item in exported:
            obj = item if inspect.isclass(item) else getattr(module, str(item), None)
            if self.is_sqlalchemy_model(obj):
                model_classes.append(obj)

        return model_classes

    def _collect_models_from_package_submodules(self, package) -> list:
        model_classes = []

        if not hasattr(package, "__path__"):
            return model_classes

        prefix = package.__name__ + "."
        for _, modname, _ in pkgutil.iter_modules(package.__path__, prefix):
            try:
                submodule = importlib.import_module(modname)
                model_classes.extend(self._collect_models_from_module(submodule))
            except Exception as ex:
                da_logger.warning(f"Could not import submodule {modname}: {ex}")

        return model_classes

    def _deduplicate_models(self, model_classes: list) -> list:
        seen = set()
        unique = []

        for cls in model_classes:
            key = f"{cls.__module__}.{cls.__name__}"
            if key not in seen:
                seen.add(key)
                unique.append(cls)

        return unique

    def get_model_classes_from_app(self, app_label: str) -> List[Type[DBBase]]:
        ap = self.get_app_config(app_label)
        module_name = f'{ap["path"]}.models'
        module = importlib.import_module(module_name)

        da_logger.debug(f"Loaded models module/package: {module}")

        model_classes = []

        # 1. Prefer __all__ if present
        model_classes.extend(self._collect_models_from_all(module))

        # 2. Fallback to direct attributes in module namespace
        if not model_classes:
            model_classes.extend(self._collect_models_from_module(module))

        # 3. If models is a package, also scan submodules
        if hasattr(module, "__path__"):
            model_classes.extend(self._collect_models_from_package_submodules(module))

        model_classes = self._deduplicate_models(model_classes)

        da_logger.debug(
            "Discovered model classes: %s".format(
            [f"{cls.__module__}.{cls.__name__}" for cls in model_classes])
        )

        return model_classes

    @staticmethod
    def is_table_present(engine, table_name: str) -> bool:
        inspector = sql_inspect(engine)
        return table_name in inspector.get_table_names()

    def get_db_connection_name_for_app(self, app_label: str, db_connection_model):
        engine = self.get_model_engine(app_label)
        url = engine.url
        db_name = url.database
        host = url.host or "localhost"
        return db_connection_model.get_connection_name(db_name, host)

    @staticmethod
    def guess_geometry_column(model_class):
        geom_cols = []

        for col in model_class.__table__.columns:
            type_name = type(col.type).__name__.lower()
            type_str = str(col.type).lower()

            if "geometry" in type_name or "geography" in type_str:
                geom_cols.append(col)

        if not geom_cols:
            return None

        # 1. explicit override
        override = getattr(model_class, "__geometry_column__", None)
        if override:
            for col in geom_cols:
                if col.name == override:
                    return col

        # 2. preferred names
        preferred = ["geom", "geometry", "shape", "wkb_geometry"]
        for name in preferred:
            for col in geom_cols:
                if col.name.lower() == name:
                    return col

        # 3. single column
        if len(geom_cols) == 1:
            return geom_cols[0]

        # 4. fallback: first one (but log warning)
        da_logger.warning(
            f"Multiple geometry columns in {model_class.__name__}: "
            f"{[c.name for c in geom_cols]}, using '{geom_cols[0].name}'"
        )

        return geom_cols[0]

    def get_geometry_column_name(self, model_class) -> str:
        geom_col = self.guess_geometry_column(model_class)

        if geom_col is None:
            raise ValueError(
                f"Geometry column is required for model '{model_class.__name__}'"
            )

        return geom_col.name

    @staticmethod
    def get_pk_cols( model_class) -> List[str]:
        """
        Returns all primary key column names.
        Supports composite primary keys.
        """
        return [col.name for col in model_class.__table__.primary_key.columns]

    @staticmethod
    def get_primary_key_columns(model_class) -> List[Column]:
        """
        Returns all primary key Column objects.
        """
        return list(model_class.__table__.primary_key.columns)

    def get_primary_column(self, model_class):
        """
        Returns the best single primary column for layer/id usage.
            Priority:
            1. explicit override: __primary_column__
            2. if single PK exists, use it
            3. preferred PK names: id, fid, gid, objectid, ogc_fid
            4. fallback to first PK column
        """
        pk_cols = self.get_primary_key_columns(model_class)

        if not pk_cols:
            raise ValueError(
                f"No primary key defined for model '{model_class.__name__}'"
            )

        # override
        override = getattr(model_class, "__primary_column__", None)
        if override:
            for col in pk_cols:
                if col.name == override:
                    return col

        # single pk
        if len(pk_cols) == 1:
            return pk_cols[0]

        # preferred names
        preferred = ["id", "fid", "gid", "objectid", "ogc_fid"]
        for name in preferred:
            for col in pk_cols:
                if col.name.lower() == name:
                    return col

        # fallback
        da_logger.warning(
            f"Multiple PK columns in {model_class.__name__}: "
            f"{[c.name for c in pk_cols]}, using '{pk_cols[0].name}'"
        )

        return pk_cols[0]

    def get_primary_column_name(self, model_class) -> str:
        """
        Returns best single primary column name.
        Raises exception if not found.
        """
        col = self.get_primary_column(model_class)

        if col is None:
            raise ValueError(
                f"Primary key column is required for model '{model_class.__name__}'"
            )

        return col.name

    @staticmethod
    def get_non_geom_columns(
            model_class,
            geom_col_name: Optional[str]
    ) -> Dict[str, str]:
        """
        Returns all non-geometry columns as {column_name: type}.
        """
        cols = {}

        for col in model_class.__table__.columns:
            # skip geometry column
            if geom_col_name is not None and col.name == geom_col_name:
                continue

            cols[col.name] = str(col.type)

        return cols