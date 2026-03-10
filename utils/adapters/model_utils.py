import importlib

from digitalarzengine.io.managers.db_manager import GeoDBManager
from sqlalchemy import insert



from app.damapql.utils.adapters.db import DBBase
from app.damapql.settings import DA_APPS


class ModelUtils:
    @staticmethod
    def get_model(model_name):
        for key in DA_APPS:

            try:
                module = importlib.import_module(f'{DA_APPS[key]["path"]}.models')
                for a in dir(module):
                    if a.lower() == model_name.lower():
                        cls = getattr(module, a)
                        if cls is not None:
                            return cls
            except Exception as e:
                # traceback.print_exc()
                print(f"ERROR: Failed to load models at '{key}' due to : {e}")
        return None

    @classmethod
    def add_model_obj(cls,model_name, row):
        model = cls.get_model(model_name)
        manager: GeoDBManager = model.manager()

        if model is not None:
            stmt = insert(model).values(**row)
            # res = manager.execute_ddl(stmt)
            result = manager.execute_stmt(stmt)
            return result
        return None

    @staticmethod
    def get_model_class_by_name(model_name: str):
        # Loop through all loaded classes
        for cls in DBBase.__subclasses__():
            if cls.__name__ == model_name:
                return cls
        raise ValueError(f"Model '{model_name}' not found among DBBase subclasses.")

