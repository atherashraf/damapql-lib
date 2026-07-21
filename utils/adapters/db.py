from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from app.damapql.utils.adapters.db_mixin import ModelHelpersMixin

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey"
}

metadata_obj = MetaData(naming_convention=convention)


# Base = declarative_base(metadata=metadata_obj)


class DBBase(DeclarativeBase, ModelHelpersMixin):
    metadata = metadata_obj

    class _Meta:
        is_spatial = True
        db_key = None
        app_label = None