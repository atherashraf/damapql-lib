import importlib
from typing import Any

from sqlalchemy.exc import IntegrityError

from digitalarzengine.io.managers.db_manager import GeoDBManager
from digitalarzengine.utils.singletons import da_logger
from sqlalchemy import insert, inspect

import settings
from app.damapql.utils.adapters.db import DBBase

from settings import DA_APPS, get_model_bases


class ModelUtils:
    @staticmethod
    def get_model(model_name):
        for key in DA_APPS:
            da_logger.info(f"working on DA_APP {key}")
            try:
                da_logger.info(f"working on DA_APP {key}",DA_APPS[key]["path"])
                module = importlib.import_module(f'{DA_APPS[key]["path"]}.models')
                da_logger.info(f"working on {key} module", module)
                for a in dir(module):
                    if a.lower() == model_name.lower():
                        cls = getattr(module, a)
                        if cls is not None:
                            return cls
            except Exception as e:
                # traceback.print_exc()
                print(f"ERROR: Failed to load models at '{key}' due to : {e}")
        return None

    @staticmethod
    def get_all_subclasses(cls):
        all_subclasses = []
        for subclass in cls.__subclasses__():
            all_subclasses.append(subclass)
            all_subclasses.extend(ModelUtils.get_all_subclasses(subclass))
        return all_subclasses

    @classmethod
    def add_model_obj(
            cls,
            model_name: str,
            row: dict[str, Any],
    ):
        model_class = cls.get_model(model_name)

        if model_class is None:
            return None

        with model_class.managed_session() as session:
            stmt = insert(model_class).values(**row)
            result = session.execute(stmt)
            session.commit()

            return result

    @staticmethod
    def get_model_class_by_name(model_name: str):
        # Loop through all loaded classes
        for base in settings.get_model_bases():
            da_logger.info(f"working on {base.__name__}")
            for cls in base.__subclasses__():
                if cls.__name__ == model_name:
                    return cls
        raise ValueError(f"Model '{model_name}' not found among DBBase subclasses.")

    @classmethod
    def get_model_by_tablename(cls, table_name: str):

        for base in get_model_bases():
            for model in cls.get_all_subclasses(base):
                if hasattr(model, "__tablename__") and model.__tablename__ == table_name:
                    return model
        raise ValueError(f"Model with table '{table_name}' not found.")

    @staticmethod
    def get_model_info(model_name: str):
        """
        Returns:

            {
                "model": ModelClass,
                "base": BaseClass,
                "app_label": "wasa",
                "app": DA_APPS["wasa"]
            }
        """

        for app_label, app in DA_APPS.items():
            try:
                module = importlib.import_module(
                    f'{app["path"]}.models'
                )

                for attr in dir(module):

                    if attr.lower() != model_name.lower():
                        continue

                    model = getattr(module, attr)

                    if not hasattr(model, "__table__"):
                        continue

                    if "base" not in app:
                        base = DBBase
                    else:
                        from app.damapql.manage import import_string
                        base = (
                            import_string(app["base"])
                            if isinstance(app["base"], str)
                            else app["base"]
                        )

                    return {
                        "model": model,
                        "base": base,
                        "app": app,
                        "app_label": app_label,
                    }

            except Exception:
                pass

        raise ValueError(
            f"Model '{model_name}' not found."
        )

    @classmethod
    def delete_model_obj(
            cls,
            model_name: str,
            pk: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Delete one model object using its actual primary key.

        Example:
            delete_model_obj(
                model_name="LayerCategory",
                pk={"id": 4},
            )
        """
        model_class = cls.get_model(model_name)

        if model_class is None:
            raise ValueError(
                f"Model '{model_name}' was not found."
            )

        mapper = inspect(model_class)
        pk_columns = list(mapper.primary_key)
        pk_names = [column.key for column in pk_columns]

        if set(pk.keys()) != set(pk_names):
            raise ValueError(
                f"Invalid primary key for '{model_name}'. "
                f"Expected: {pk_names}"
            )

        # Convert incoming values to the PK column's Python type.
        identity_values: list[Any] = []

        for column in pk_columns:
            value = pk[column.key]

            try:
                python_type = column.type.python_type
                value = python_type(value)
            except (AttributeError, TypeError, ValueError):
                pass

            identity_values.append(value)

        identity: Any = (
            identity_values[0]
            if len(identity_values) == 1
            else tuple(identity_values)
        )

        with model_class.managed_session() as session:
            obj = session.get(model_class, identity)

            if obj is None:
                raise LookupError(
                    f"{model_name} with primary key {pk} was not found."
                )

            session.delete(obj)

            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise

        return {
            "status": "success",
            "msg": "row deleted",
            "model_name": model_class.__name__,
            "pk": pk,
        }