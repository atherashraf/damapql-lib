from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Union
from datetime import datetime

import pandas as pd
import sqlalchemy as db
import unicodedata
from geopandas import GeoDataFrame
from pyproj import Transformer
from sqlalchemy import Column, Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy_utils import ChoiceType

from app.damapql.dch.api_enum import AccessibilityType, DataModel, ModelType
from app.damapql.dch.dtypes import (DBConnectionType, LayerAccessibilityType, LayerAccessibilityParams,
                                    DBAccessibilityParams, URLAccessibilityParams, MinioAccessibilityParams,
                                    S3AccessibilityParams, FileAccessibilityParams, LayerCategoryType, LayerInfoType,
                                    LayerQualityLevelType, MapInfoType, )
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from app.damapql.settings import IS_LOCAL, MEDIA_BASE, DCH_DATA_DB_KEY
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.deps import ExternalEngineRegistry, get_minio_manager
from app.damapql.utils.adapters.services.db_query import DBQuery
from app.damapql.utils.crypto import CryptoUtils
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger


class LayerCategory(DBBase):
    __tablename__ = "dch_layer_category"

    __table_args__ = (db.UniqueConstraint("main", "sub", name="uq_dch_layer_category_main_sub", ),)

    id = db.Column(db.INTEGER, primary_key=True, nullable=False)
    main = db.Column(db.VARCHAR, nullable=False)
    sub = db.Column(db.VARCHAR, nullable=True)

    __pydantic__ = LayerCategoryType

    def __repr__(self):
        return f"{self.main} - {self.sub}" if self.sub else self.main

    @property
    def slug(self) -> str:
        text = f"{self.main}-{self.sub}" if self.sub else self.main
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^\w\s-]", "", text).strip().lower()
        return re.sub(r"[-\s]+", "-", text)

    @classmethod
    def resolve_category(cls, cat_id: Optional[int] = None, main: Optional[str] = None, sub: Optional[str] = None,
                         session: Optional[Session] = None) -> Optional["LayerCategory"]:
        with cls.managed_session(session) as session:
            if cat_id is not None:
                return session.query(cls).filter(cls.id == cat_id).first()
            if not main:
                return None
            main = main.strip()
            sub = sub.strip() if sub else None
            query = session.query(cls).filter(cls.main == main)
            if sub is not None:
                query = query.filter(cls.sub == sub)
            else:
                query = query.filter(cls.sub.is_(None))
            return query.first()

    @classmethod
    def create_category(cls, main: str, sub: Optional[str] = None,
                        session: Optional[Session] = None, ) -> "LayerCategory":

        with cls.managed_session(session) as session:
            existing = cls.resolve_category(session=session, main=main, sub=sub, )

            if existing:
                return existing

            category = cls(main=main.strip(), sub=sub.strip() if sub else None, )

            session.add(category)
            session.commit()
            session.refresh(category)

            return category


class DBConnection(DBBase):
    __tablename__ = "dch_db_connection"

    id = Column(db.INTEGER, primary_key=True, autoincrement=True)
    name = Column(db.VARCHAR(100), nullable=False, unique=True)
    params = Column(db.JSON, nullable=False)

    __pydantic__ = DBConnectionType

    def __str__(self):
        return self.name

    @classmethod
    def get_or_create_from_db_key(cls, db_key: str = DCH_DATA_DB_KEY) -> DBConnection:
        """
        Resolves or auto-registers a DBConnection record in `dch_db_connection`
        from a configured key in `settings.DATABASES` (e.g. DCH_DATA_DB_KEY).
        """
        from app.damapql.settings import DCH_DATA_DB_KEY
        from settings import DATABASES

        target_key = db_key or DCH_DATA_DB_KEY
        if target_key not in DATABASES:
            raise KeyError(f"Database key '{target_key}' is not configured in settings.DATABASES.")

        db_config = DATABASES[target_key]
        db_name = db_config["name"]
        host = "localhost" if os.environ.get("DATA_HOST") == db_config["host"] and IS_LOCAL else db_config["host"]
        con_name = cls.get_connection_name(db_name, host)

        with cls.managed_session() as session:
            try:
                db_conn = session.execute(
                    select(cls).where(cls.name == con_name)
                ).scalar_one_or_none()

                if db_conn is None:
                    encrypted_password = CryptoUtils().encrypt_txt(db_config["password"])
                    db_conn = cls(
                        name=con_name,
                        params={
                            "engine": db_config.get("engine", "postgresql"),
                            "host": host,
                            "port": str(db_config.get("port", "5432")),
                            "user": db_config["user"],
                            "password": encrypted_password,
                            "name": db_name,
                        },
                    )
                    session.add(db_conn)
                    session.commit()
                    session.refresh(db_conn)

                return db_conn
            except SQLAlchemyError:
                session.rollback()
                raise

    @classmethod
    def create_from_dict(cls, db_config: dict) -> DBConnection:
        """Creates a DBConnection from a configuration dictionary mapping."""
        engine = db_config.get("engine")
        host = db_config.get("host")
        port = db_config.get("port")
        user = db_config.get("user")
        password = db_config.get("password")
        db_name = db_config.get("name")

        return cls.get_db_connection(con_name=cls.get_connection_name(db_name, host), engine=engine, host=host,
                                     port=port, user=user, password=password, db_name=db_name, )

    @staticmethod
    def get_connection_name(db_name: str, host: str) -> str:
        return f"{db_name}_{host}"

    @classmethod
    def get_db_connection(cls, con_name: str, engine: str = None, host: str = None, port: str = None, user: str = None,
                          password: str = None, db_name: str = None, ) -> DBConnection:
        with cls.managed_session() as session:
            try:
                stmt = select(cls).where(cls.name == con_name)
                db_conn = session.execute(stmt).scalar_one_or_none()

                if db_conn is None:
                    if not all([engine, host, port, user, password, db_name]):
                        raise ValueError("All connection parameters must be provided to create a DB connection.")

                    encrypted_password = CryptoUtils().encrypt_txt(password)
                    host = "localhost" if os.environ.get("DATA_HOST") == host and IS_LOCAL else host

                    db_conn = cls()
                    db_conn.name = con_name
                    db_conn.params = {"engine": engine, "host": host, "port": port, "user": user,
                                      "password": encrypted_password, "name": db_name, }
                    session.add(db_conn)
                    session.commit()

                return db_conn
            except SQLAlchemyError:
                session.rollback()
                raise

    def get_sqlalchemy_engine(self) -> Engine:
        return ExternalEngineRegistry.get(self.name, self.params.copy())


class LayerQualityLevel(DBBase):
    __tablename__ = "dch_quality_level"

    id = mapped_column("id", db.INTEGER, primary_key=True, autoincrement=True)
    level = mapped_column("level", db.INTEGER, nullable=False)
    name = mapped_column("name", db.VARCHAR(50), nullable=False)
    description = mapped_column("description", db.TEXT, nullable=True)

    __pydantic__ = LayerQualityLevelType


class LayerAccessibility(DBBase):
    __tablename__ = "dch_layer_accessibility"

    id = Column(db.INTEGER, primary_key=True, autoincrement=True, )

    accessibility_type = Column(ChoiceType(AccessibilityType.choices(), impl=db.VARCHAR(50), ), nullable=False, )

    # IMPORTANT:
    # Always store plain dict/JSON here,
    # never a Pydantic BaseModel instance.
    params = Column(db.JSON, nullable=False, )

    # 1:1 relationship mapping (scalar, not list)
    layer_info: Mapped["LayerInfo"] = relationship(back_populates="layer_accessibility", uselist=False)

    __pydantic__ = LayerAccessibilityType

    # ================================================================
    # Accessibility Type Helpers
    # ================================================================

    @property
    def accessibility_type_name(self) -> str:
        """
        Normalize ChoiceType / Enum / string accessibility type
        into a comparable string.
        """
        return getattr(self.accessibility_type, "name", str(self.accessibility_type), )

    def _is_accessibility_type(self, accessibility_type: AccessibilityType, ) -> bool:
        return self.accessibility_type_name in {accessibility_type.name, accessibility_type.value, }

    @property
    def is_db(self) -> bool:
        return self._is_accessibility_type(AccessibilityType.DB)

    @property
    def is_url(self) -> bool:
        return self._is_accessibility_type(AccessibilityType.URL)

    @property
    def is_file(self) -> bool:
        return self._is_accessibility_type(AccessibilityType.File)

    @property
    def is_minio(self) -> bool:
        return self._is_accessibility_type(AccessibilityType.Minio)

    @property
    def is_s3(self) -> bool:
        return self._is_accessibility_type(AccessibilityType.S3)

    # ================================================================
    # Polymorphic Params
    # ================================================================

    @property
    def accessibility_params(self) -> LayerAccessibilityParams:
        """
        Convert raw database JSON params into the correct
        polymorphic Pydantic model.
        """

        params = dict(self.params or {})

        if self.is_db:
            return DBAccessibilityParams.model_validate(params)

        if self.is_url:
            return URLAccessibilityParams.model_validate(params)

        if self.is_minio:
            return MinioAccessibilityParams.model_validate(params)

        if self.is_s3:
            return S3AccessibilityParams.model_validate(params)

        if self.is_file:
            return FileAccessibilityParams.model_validate(params)

        raise ValueError(f"Unsupported accessibility type: "
                         f"{self.accessibility_type}")

    @staticmethod
    def serialize_params(params: LayerAccessibilityParams, ) -> dict:
        """
        Convert Pydantic accessibility params into JSON-safe dict.
        """

        return params.model_dump(mode="json", exclude_none=True, )

    # ================================================================
    # Storage Helpers
    # ================================================================

    @hybrid_property
    def model_name(self) -> Optional[str]:
        return (self.params or {}).get("model_name")

    @model_name.expression
    def model_name(cls):
        return cls.params.op("->>")("model_name")

    @property
    def s3_bucket(self) -> Optional[str]:
        if not (self.is_s3 or self.is_minio):
            return None

        params = self.accessibility_params

        if isinstance(params, (S3AccessibilityParams, MinioAccessibilityParams,), ):
            return params.bucket

        return None

    @property
    def s3_object_name(self) -> Optional[str]:
        if not (self.is_s3 or self.is_minio):
            return None

        params = self.accessibility_params

        if isinstance(params, (S3AccessibilityParams, MinioAccessibilityParams,), ):
            return params.object_name

        return None

    @property
    def s3_uri(self) -> Optional[str]:
        if not (self.is_s3 or self.is_minio):
            return None

        params = self.accessibility_params

        if isinstance(params, (S3AccessibilityParams, MinioAccessibilityParams,), ):
            return params.s3_uri

        return None

    @property
    def file_path(self, ) -> Optional[Union[Path, str]]:
        """
        Resolve local file accessibility path.

        media_rel:
            MEDIA_BASE / relative/path/file.tif

        absolute:
            returns original absolute path

        url / ftp / s3:
            returns original string
        """

        if not self.is_file:
            return None

        params = self.accessibility_params

        if not isinstance(params, FileAccessibilityParams, ):
            return None

        resolved = params.resolve_file_path(str(MEDIA_BASE))

        if params.path_base in {"media_rel", "absolute", }:
            return Path(resolved)

        return resolved

    # ================================================================
    # Factory: S3
    # ================================================================

    @classmethod
    def create_s3_accessibility(cls, bucket: str, object_name: str, spectral_res: int = 1, spatial_res: Any = None,
                                rad_res: str = "8-bit", ) -> LayerAccessibility:
        object_name = object_name.lstrip("/")
        access_params = S3AccessibilityParams(bucket=bucket, object_name=object_name, spectral_resolution=spectral_res,
                                              spatial_resolution=spatial_res, radiometric_resolution=rad_res, )
        params = cls.serialize_params(access_params)

        with cls.managed_session() as session:
            la = cls(accessibility_type=AccessibilityType.S3.value, params=params, )
            session.add(la)
            session.commit()
            session.refresh(la)
            return la

    # ================================================================
    # Factory: MinIO
    # ================================================================

    @classmethod
    def create_minio_accessibility(cls, bucket: str, object_name: str, spectral_res: Optional[int] = None,
                                   spatial_res: Any = None, rad_res: Optional[str] = None, ) -> LayerAccessibility:
        object_name = object_name.lstrip("/")
        access_params = MinioAccessibilityParams(bucket=bucket, object_name=object_name,
                                                 spectral_resolution=spectral_res, spatial_resolution=spatial_res,
                                                 radiometric_resolution=rad_res, )
        params = cls.serialize_params(access_params)

        with cls.managed_session() as session:
            la = cls(accessibility_type=AccessibilityType.Minio.value, params=params, )
            session.add(la)
            session.commit()
            session.refresh(la)
            return la

    # ================================================================
    # Factory: File
    # ================================================================

    @classmethod
    def create_file_accessibility(cls, file_path: str, path_base: str = "media_rel", ) -> LayerAccessibility:
        access_params = FileAccessibilityParams(file_path=file_path, path_base=path_base, )
        params = cls.serialize_params(access_params)

        with cls.managed_session() as session:
            la = cls(accessibility_type=AccessibilityType.File.value, params=params, )
            session.add(la)
            session.commit()
            session.refresh(la)
            return la

    # ================================================================
    # Factory: Database
    # ================================================================

    @classmethod
    def create_db_accessibility(cls, table_name: str, db_conn: Union[DBConnection, str], columns: dict,
                                pk_cols: list, ) -> LayerAccessibility:
        cols = []
        g_col = None

        for column_name, column_type in columns.items():
            type_str = str(column_type).lower()
            if "geometry" in type_str:
                g_col = column_name
            else:
                cols.append({"name": column_name, "d_type": ManageDataFrameUtils.getColGenericType(type_str), })

        conn_name = db_conn.name if hasattr(db_conn, "name") else db_conn
        access_params = DBAccessibilityParams(conn_name=conn_name, table_name=table_name, columns=cols, g_col=g_col,
                                              pk_cols=pk_cols, )
        params = cls.serialize_params(access_params)

        with cls.managed_session() as session:
            la = cls(accessibility_type=AccessibilityType.DB.value, params=params, )
            session.add(la)
            session.commit()
            session.refresh(la)
            return la

    # ================================================================
    # Factory: URL
    # ================================================================

    @classmethod
    def create_url_accessibility(cls, url: str, url_type: Optional[str] = None, ) -> LayerAccessibility:
        access_params = URLAccessibilityParams(url=url, url_type=url_type, )
        params = cls.serialize_params(access_params)

        with cls.managed_session() as session:
            la = cls(accessibility_type=AccessibilityType.URL.value, params=params, )
            session.add(la)
            session.commit()
            session.refresh(la)
            return la

    # ================================================================
    # JSON Hybrid Properties
    # ================================================================

    @hybrid_property
    def table_name(self) -> Optional[str]:
        return (self.params or {}).get("table_name")

    @table_name.expression
    def table_name(cls):
        return cls.params.op("->>")("table_name")

    @hybrid_property
    def conn_name(self) -> Optional[str]:
        return (self.params or {}).get("conn_name")

    @conn_name.expression
    def conn_name(cls):
        return cls.params.op("->>")("conn_name")

    @hybrid_property
    def url(self) -> Optional[str]:
        return (self.params or {}).get("url")

    @url.expression
    def url(cls):
        return cls.params.op("->>")("url")

    @hybrid_property
    def url_type(self) -> Optional[str]:
        return (self.params or {}).get("url_type")

    @url_type.expression
    def url_type(cls):
        return cls.params.op("->>")("url_type")

    # ================================================================
    # DB Metadata Helpers
    # ================================================================

    @property
    def pk_cols(self) -> list:
        return (self.params or {}).get("pk_cols", [], )

    @property
    def cols_name(self) -> list[str]:
        cols = (self.params or {}).get("columns") or []
        if isinstance(cols, str):
            import json
            try:
                cols = json.loads(cols)
            except Exception:
                return []

        result: list[str] = []
        for column in cols:
            if isinstance(column, dict) and column.get("name"):
                result.append(column["name"])
            elif isinstance(column, str) and column:
                result.append(column)
        return result

    @property
    def cols_type(self) -> list:
        cols = (self.params or {}).get("columns") or []
        if isinstance(cols, str):
            import json
            try:
                cols = json.loads(cols)
            except Exception:
                return []

        return [
            column.get("d_type", "string") if isinstance(column, dict) else "string"
            for column in cols
        ]

    @property
    def geometry_col(self) -> Optional[str]:
        return (self.params or {}).get("g_col")

    # ================================================================
    # Table Validation
    # ================================================================

    def get_table_columns(self, session: Session, schema: str = "public", ) -> set[str]:

        if not self.table_name:
            return set()

        sql = text("""
                   SELECT column_name
                   FROM information_schema.columns
                   WHERE table_schema = :schema
                     AND table_name = :table_name
                   """)

        rows = session.execute(sql, {"schema": schema, "table_name": self.table_name, }, ).fetchall()

        return {row[0] for row in rows}

    def get_valid_cols(self, session: Session, schema: str = "public", ) -> list[str]:

        db_cols = self.get_table_columns(session=session, schema=schema, )

        metadata_cols = set(self.cols_name or [])

        valid_cols = (metadata_cols.intersection(db_cols))

        missing_cols = (metadata_cols - db_cols)

        if missing_cols:
            da_logger.warning("[LayerColumnMismatch] "
                              f"table={self.table_name}, "
                              f"missing={sorted(missing_cols)}")

        return sorted(valid_cols)

    # ================================================================
    # External DB Engine
    # ================================================================

    def get_external_engine(self, ) -> Optional[Engine]:

        if not self.is_db:
            return None

        if not self.conn_name:
            return None

        db_conn = (DBConnection.get_db_connection(con_name=self.conn_name))

        if not db_conn:
            return None

        return (db_conn.get_sqlalchemy_engine())


class LayerInfo(DBBase):
    __tablename__ = "dch_layer_info"

    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    app_label = Column(db.VARCHAR(100), nullable=False, default="dch")
    title = Column(db.String, nullable=False)
    layer_name = Column(db.VARCHAR(255), nullable=False, unique=True)

    srid = Column(db.INTEGER, nullable=True)
    proj4 = Column(db.TEXT, nullable=True)

    extent = Column(db.ARRAY(item_type=db.Float()), nullable=True)
    extent_3857 = Column(db.ARRAY(item_type=db.Float()), nullable=True)

    data_model = Column(ChoiceType(DataModel.choices(), impl=db.VARCHAR(50)), nullable=False, )
    model_type = Column(ChoiceType(ModelType.choices(), impl=db.VARCHAR(50)), nullable=False, )

    geom_type = Column(db.ARRAY(db.VARCHAR(100)), nullable=True)
    layer_styling = Column(db.JSON, nullable=True)
    zoom_range = Column(db.ARRAY(item_type=db.Integer), nullable=False, default=[0, 30])

    uploaded_by = Column(db.Integer, nullable=True)
    uploaded_date = Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    temporal_res = Column(db.DATE, nullable=True)
    extra_metadata = Column(db.JSON, nullable=True)

    category_id = Column(db.ForeignKey(LayerCategory.id), name="category_id", nullable=True)
    layer_category = relationship("LayerCategory")

    # Enforce database-level 1:1
    layer_access_id = Column(db.ForeignKey(LayerAccessibility.id), name="layer_access_id", nullable=False, unique=True)
    layer_accessibility: Mapped[LayerAccessibility] = relationship(back_populates="layer_info")

    quality_level_id = Column(db.ForeignKey(LayerQualityLevel.id), name="quality_level_id", nullable=True)
    quality_level = relationship("LayerQualityLevel")

    data_url = Column(db.TEXT, nullable=True)
    layer_setting = Column(db.JSON, nullable=True)
    format = Column(db.VARCHAR(50), nullable=False, default="COG")
    date_range_url = Column(db.TEXT, nullable=True)
    model_name = Column(db.VARCHAR(100), nullable=True)

    __pydantic__ = LayerInfoType

    def __str__(self):
        return self.title or self.layer_name or self.uuid

    @property
    def pk_concat_char(self) -> str:
        """
        Used by layers router when splitting composite PK values.
        Default stays backward-compatible.
        """
        return (self.layer_setting or {}).get("pkConcatChar", "-")

    @property
    def pk_cols_as_pk(self) -> str:
        pk_cols = self.layer_accessibility.pk_cols
        pk_col = ""
        for col in pk_cols:
            pk_col += f"{col}" if pk_col == "" else f" || '{self.pk_concat_char}' || {col}"
        pk_col += " as pk"
        return pk_col

    @classmethod
    def get_layer_info_by_uuid(cls, uuid: str,session:Session=None) -> Optional[LayerInfo]:
        with cls.managed_session() as session:
            stmt = (select(cls).options(selectinload(cls.layer_accessibility), selectinload(cls.layer_category),
                                        selectinload(cls.quality_level), ).where(cls.uuid == uuid))
            return session.execute(stmt).scalar_one_or_none()

    def _get_default_attribute_cols(self, access: LayerAccessibility) -> list[str]:
        pk_cols = access.pk_cols or []
        g_col = (access.geometry_col or "").lower()
        raw_cols = access.cols_name or []
        clean_cols = list(pk_cols) + [c for c in raw_cols if
                                      c and c.lower() not in {"extent", g_col, "geojson", "geom", "geometry"}]
        seen = set()
        return [c for c in clean_cols if c and not (c in seen or seen.add(c))]

    def to_layer_gdf(self, cols=(), g_col: str | None = None, extent_3857=None, where_clause: str | None = None,
                     limit: int = -1, offset: int = 0, session: Session = None,
                     min_intersection_ratio: float = 0.90, ) -> GeoDataFrame:
        """
        Return GeoDataFrame for vector DB layers.

        Supports:
        - optional attribute column selection
        - optional where_clause
        - optional bbox filter in EPSG:3857
        - optional minimum intersection ratio for bbox filtering
          - lines: intersection length / feature length
          - polygons: intersection area / feature area
          - points: normal ST_Intersects
        - SQL-level LIMIT / OFFSET
        """
        try:
            if not self.data_model or self.data_model.code != DataModel.V.name:
                return GeoDataFrame()

            access = self.layer_accessibility
            if not access or not access.accessibility_type:
                return GeoDataFrame()

            if access.accessibility_type.code != AccessibilityType.DB.name:
                return GeoDataFrame()

            engine = access.get_external_engine()
            if engine is None:
                return GeoDataFrame()

            g_col = g_col or access.geometry_col
            if not g_col or not access.table_name:
                return GeoDataFrame()

            srid = self.srid or 4326
            if not cols:
                cols = self._get_default_attribute_cols(access)

            safe_table = DBQuery.safe_ident(access.table_name)
            safe_geom = DBQuery.safe_ident(g_col)
            safe_cols = ", ".join(DBQuery.safe_ident(c) for c in cols) if cols else ""
            sql = f"SELECT {safe_geom}, {safe_cols} FROM {safe_table}" if safe_cols else f"SELECT {safe_geom} FROM {safe_table}"

            where_clauses = []
            if where_clause:
                clean_where = where_clause.strip()
                if clean_where.lower().startswith("where"):
                    clean_where = clean_where[5:].strip()
                if clean_where:
                    where_clauses.append(f"({clean_where})")

            is_postgres = engine.dialect.name == "postgresql"
            if is_postgres and extent_3857 and isinstance(extent_3857, (tuple, list)) and len(extent_3857) == 4:
                minx, miny, maxx, maxy = [float(v) for v in extent_3857]
                ratio = max(0.0, min(float(min_intersection_ratio), 1.0))
                bbox_sql = f"ST_Transform(ST_SetSRID(ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}), 3857), {int(srid)})"

                where_clauses.append(f"""
                    ST_Intersects({safe_geom}, {bbox_sql}) AND (
                        CASE
                            WHEN GeometryType({safe_geom}) IN ('LINESTRING', 'MULTILINESTRING') AND ST_Length({safe_geom}) > 0
                            THEN ST_Length(ST_Intersection({safe_geom}, {bbox_sql})) / ST_Length({safe_geom}) >= {ratio}
                            WHEN GeometryType({safe_geom}) IN ('POLYGON', 'MULTIPOLYGON') AND ST_Area({safe_geom}) > 0
                            THEN ST_Area(ST_Intersection({safe_geom}, {bbox_sql})) / ST_Area({safe_geom}) >= {ratio}
                            ELSE ST_Intersects({safe_geom}, {bbox_sql})
                        END
                    )
                    """.strip())

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)
            if limit is not None and limit >= 0:
                sql += f" LIMIT {int(limit)}"
            if offset and offset > 0:
                sql += f" OFFSET {int(offset)}"

            with Session(engine) as session:
                return DBQuery.execute_query_as_gdf(session, sql, geom_col=g_col, crs=f"EPSG:{srid}")

        except Exception as e:
            da_logger.exception(f"Failed to load layer GDF for {self.layer_name}: {e}")
            return GeoDataFrame()

    def count_layer_rows(self, where_clause: str | None = None, extent_3857=None, g_col: str | None = None) -> int:
        try:
            if not self.data_model or self.data_model.code != DataModel.V.name:
                return 0

            access = self.layer_accessibility
            if not access or not access.accessibility_type or access.accessibility_type.code != AccessibilityType.DB.name:
                return 0

            engine = access.get_external_engine()
            if engine is None or not access.table_name:
                return 0

            g_col = g_col or access.geometry_col
            srid = self.srid or 3857
            safe_table = DBQuery.safe_ident(access.table_name)
            sql = f"SELECT COUNT(*) FROM {safe_table}"
            where_clauses: list[str] = []

            if where_clause:
                clean_where = where_clause.strip()
                if clean_where.lower().startswith("where"):
                    clean_where = clean_where[5:].strip()
                if clean_where:
                    where_clauses.append(f"({clean_where})")

            is_postgres = engine.dialect.name == "postgresql"
            if is_postgres and g_col and extent_3857 and isinstance(extent_3857, (tuple, list)) and len(
                    extent_3857) == 4:
                minx, miny, maxx, maxy = extent_3857
                safe_geom = DBQuery.safe_ident(g_col)
                where_clauses.append(
                    f"ST_Intersects({safe_geom}, ST_Transform(ST_SetSRID(ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}), 3857), {srid}))")

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            with Session(engine) as session:
                return int(session.execute(text(sql)).scalar_one() or 0)

        except Exception as e:
            da_logger.exception(f"Error counting rows for layer {self.layer_name}: {e}")
            return 0

    def to_layer_df(self, cols=(), where_clause: str | None = None, limit: int = -1, offset: int = 0) -> pd.DataFrame:
        try:
            if not self.data_model:
                return pd.DataFrame()

            # Vector layer
            if self.data_model.code == DataModel.V.name:
                access = self.layer_accessibility
                if not access or not access.accessibility_type or access.accessibility_type.code != AccessibilityType.DB.name:
                    return pd.DataFrame()

                engine = access.get_external_engine()
                if engine is None or not access.table_name:
                    return pd.DataFrame()

                if not cols:
                    cols = self._get_default_attribute_cols(access)
                if not cols:
                    return pd.DataFrame()

                safe_table = DBQuery.safe_ident(access.table_name)
                select_cols = ", ".join(DBQuery.safe_ident(c) for c in cols)
                sql = f"SELECT {select_cols} FROM {safe_table}"

                if where_clause:
                    clean_where = where_clause.strip()
                    sql += f" {clean_where}" if clean_where.lower().startswith("where") else f" WHERE {clean_where}"

                if limit is not None and limit >= 0:
                    sql += f" LIMIT {int(limit)}"
                if offset and offset > 0:
                    sql += f" OFFSET {int(offset)}"

                with Session(engine) as session:
                    return DBQuery.execute_stmt_as_df(session, sql)

            # Raster layer
            if self.data_model.code == DataModel.R.name:
                raster = self.to_raster()
                if raster is None:
                    return pd.DataFrame()

                band_count = raster.get_spectral_resolution()
                if band_count == 1:
                    res = BandProcess.get_value_count_data(raster.get_data_array(1), raster.get_nodata_value())
                    return pd.DataFrame.from_records(res)

                summaries = [BandProcess.get_summary_data(raster.get_data_array(b), nodata=raster.get_nodata_value())
                             for b in range(1, band_count + 1)]
                return pd.DataFrame(summaries)

            return pd.DataFrame()

        except Exception as e:
            da_logger.exception(f"Error converting Layer to DataFrame: {e}")
            return pd.DataFrame()

    def recalculate_extent_3857(self):
        """Syncs extent_3857 from extent using self.srid."""
        if not self.extent or len(self.extent) != 4:
            self.extent_3857 = None
            return

        native_srid = self.srid or 3857
        if native_srid == 3857:
            self.extent_3857 = [float(x) for x in self.extent]
        else:
            try:
                transformer = Transformer.from_crs(f"EPSG:{native_srid}", "EPSG:3857", always_xy=True)
                x1, y1 = transformer.transform(self.extent[0], self.extent[1])
                x2, y2 = transformer.transform(self.extent[2], self.extent[3])
                self.extent_3857 = [float(x1), float(y1), float(x2), float(y2)]
            except Exception as tf_err:
                da_logger.error(f"Failed to transform extent to 3857 for {self.layer_name}: {tf_err}")
                self.extent_3857 = None

    def to_cog(self) -> COGRaster:
        """Opens COG raster from either MinIO (presigned URL) or local file."""
        try:
            if not self.data_model or self.data_model.code not in (DataModel.R.name, DataModel.R.value):
                raise ValueError(f"Invalid data model: {self.data_model}")

            access = self.layer_accessibility
            if not access:
                raise ValueError("Layer accessibility is not set")

            # 1. MinIO/S3 Storage
            if access.is_minio:
                minio_manager = get_minio_manager()
                return COGRaster.open_from_minio(access.accessibility_params,
                                                 minio_manager=minio_manager)  # # url = minio_manager.generate_presigned_url(  #     bucket_name=access.s3_bucket,  #     object_name=access.s3_object_name,  #     expires_minutes=30,  # )  # return COGRaster.open_cog(url) if url else None

            # 2. Local File Storage fallback
            file_path = access.file_path
            if not file_path or not os.path.exists(str(file_path)):
                raise ValueError(f"File path not set or does not exist: {file_path}")
            return COGRaster.open_from_local(str(file_path))

        except Exception as e:
            da_logger.exception(f"Error opening COG for {self.layer_name}: {e}")
            return None

    def to_raster(self) -> RioRaster:
        """Provides open raster handle for statistics or summaries."""
        cog_raster = self.to_cog()
        return cog_raster.get_rio_raster()

    def get_row_detail(self, col_name: str, col_value: Any):
        try:
            if not self.data_model or self.data_model.code != DataModel.V.name:
                return []

            access = self.layer_accessibility
            if not access or not access.accessibility_type or access.accessibility_type.code != AccessibilityType.DB.name:
                return []

            if not access.table_name or col_name not in set(access.cols_name or []):
                return []

            engine = access.get_external_engine()
            if engine is None:
                return []

            table = DBQuery.safe_ident(access.table_name)
            safe_col = DBQuery.safe_ident(col_name)
            sql = db.text(f"SELECT * FROM {table} WHERE {safe_col} = :col_value LIMIT 50")

            with Session(engine) as session:
                return session.execute(sql, {"col_value": col_value}).mappings().all()

        except Exception as e:
            da_logger.exception(f"Error fetching row detail for {self.layer_name}: {e}")
            return []

    def update_extent(self, new_bounds: list, overwrite: bool = False):
        with self.managed_session() as session:
            session.add(self)
            new_b = [float(x) for x in new_bounds]

            if overwrite or not self.extent or len(self.extent) != 4:
                self.extent = new_b
            else:
                self.extent = [min(self.extent[0], new_b[0]), min(self.extent[1], new_b[1]),
                               max(self.extent[2], new_b[2]), max(self.extent[3], new_b[3]), ]

            try:
                native_srid = self.srid or 3857
                if native_srid == 3857:
                    self.extent_3857 = self.extent
                else:
                    transformer = Transformer.from_crs(f"EPSG:{native_srid}", "EPSG:3857", always_xy=True)
                    x1, y1 = transformer.transform(self.extent[0], self.extent[1])
                    x2, y2 = transformer.transform(self.extent[2], self.extent[3])
                    self.extent_3857 = [x1, y1, x2, y2]
            except Exception as tf_err:
                da_logger.error(f"Failed to transform extent to 3857 for {self.layer_name}: {tf_err}")

            flag_modified(self, "extent")
            flag_modified(self, "extent_3857")

            session.commit()




class MapInfo(DBBase):
    __tablename__ = "dch_map_info"

    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    title = Column(db.VARCHAR(100), nullable=False, unique=True)
    extent = Column(db.ARRAY(item_type=db.Float), nullable=False)
    layers = Column(db.JSON, nullable=False)
    srid = Column(db.INTEGER, nullable=False, default=3857)
    units = Column(db.VARCHAR(10), nullable=False, default="m")
    description = Column(db.TEXT, nullable=True)
    navigation_setting = Column(db.JSON, nullable=True)

    __pydantic__ = MapInfoType

    def to_object(self):
        obj = super().to_object()
        data = obj.model_dump() if hasattr(obj, "model_dump") else vars(obj)

        groups = []
        clean_layers = []
        for layer in data.get("layers") or []:
            if isinstance(layer, dict) and layer.get("isGroupMeta"):
                groups = layer.get("groups") or []
            else:
                clean_layers.append(layer)

        data["groups"] = groups
        data["layers"] = clean_layers
        return self.__pydantic__.model_validate(data)

    def get_layer_info(self, uuid: str):
        for layer in self.layers:
            if layer.get("isGroupMeta"):
                continue
            if layer.get("uuid") == uuid:
                return layer
        return None

    @classmethod
    def get_all_map_info(cls, limit: Optional[int] = None) -> pd.DataFrame:
        stmt = db.select(cls)
        if limit is not None:
            stmt = stmt.limit(limit)
        with cls.get_engine().connect() as con:
            rs = con.execute(stmt)
            df = pd.DataFrame(rs.fetchall())
            if not df.empty:
                df.columns = rs.keys()
            else:
                df = pd.DataFrame({col: [] for col in rs.keys()})
            return df


class TempLayerInfo(DBBase):
    """
    Dedicated catalog for user-uploaded temporary scratchpads, meeting previews,
    and ephemeral GIS datasets. Completely decoupled from permanent `LayerInfo`.
    """
    __tablename__ = "temp_layer_info"

    id = db.Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = db.Column(db.VARCHAR(100), unique=True, nullable=False, index=True)
    title = db.Column(db.VARCHAR(255), nullable=False)

    # User Scoping pointing directly to auth_user table
    user_id = db.Column(
        db.BIGINT,
        db.ForeignKey("auth_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Geometry & Spatial Extents
    geom_type = db.Column(db.VARCHAR(50), nullable=False)  # Point, LineString, Polygon
    srid = db.Column(db.INTEGER, default=4326)
    extent = db.Column(db.JSON, nullable=True)               # [minx, miny, maxx, maxy] (EPSG:4326)
    extent_3857 = db.Column(db.JSON, nullable=True)          # Web Mercator bounds for OpenLayers fitting
    feature_count = db.Column(db.INTEGER, default=0)

    # Storage Metadata (FlatGeobuf in MinIO / Local Disk)
    format = db.Column(db.VARCHAR(50), default="FlatGeobuf")
    storage_params = db.Column(db.JSON, nullable=False, default=dict)

    # Client-Side Symbology (IFeatureStyle)
    layer_styling = db.Column(db.JSON, nullable=True)

    # Lifespan & Automatic Expiry
    created_at = db.Column(db.TIMESTAMP, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.TIMESTAMP, nullable=True)

    @property
    def is_expired(self) -> bool:
        if self.expires_at:
            return datetime.utcnow() > self.expires_at
        return False