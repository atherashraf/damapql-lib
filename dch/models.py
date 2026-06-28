import os
from typing import Union, Optional

import pandas as pd
import sqlalchemy as db
from digitalarzengine.io.cog_raster import COGRaster


from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger
from geopandas import GeoDataFrame
from pyproj import Transformer
from sqlalchemy.exc import SQLAlchemyError


from sqlalchemy import Column, func, select, Engine, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import mapped_column, Mapped, relationship, Session, selectinload
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy_utils import ChoiceType
from app.damapql.dch.api_enum import AccessibilityType, DataModel, ModelType
from app.damapql.dch.dtypes import LayerCategoryType, DBConnectionType, LayerAccessibilityType, LayerQualityLevelType, \
    LayerInfoType, MapInfoType
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.deps import ExternalEngineRegistry
from app.damapql.utils.adapters.services.db_query import DBQuery
from app.damapql.utils.crypto import CryptoUtils

from app.damapql.settings import IS_LOCAL, DCH_MEDIA_DIR
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION_NAME



class LayerCategory(DBBase):
    __tablename__ = "dch_layer_category"

    id = db.Column(db.INTEGER, primary_key=True, nullable=False)
    main = db.Column(db.VARCHAR, nullable=False)
    sub = db.Column(db.VARCHAR)
    __pydantic__ = LayerCategoryType

    def __repr__(self):
        if self.sub:
            return f"{self.main} - {self.sub}"
        return self.main

class DBConnection(DBBase):
    __tablename__ = "dch_db_connection"
    id = Column(db.INTEGER, primary_key=True, autoincrement=True)
    name = Column(db.VARCHAR(100), nullable=False, unique=True)
    params = Column(db.JSON, nullable=False)
    __pydantic__ = DBConnectionType

    def __str__(self):
        return self.name

    @classmethod
    def create_from_dict(cls, db_config: dict) -> 'DBConnection':
        """
        Creates a DBConnection from a dictionary mapping (e.g., {"wasa_data": {...}})
        """
        # 1. Extract the name (the key) and the config (the value)
        # Assumes the dict has exactly one entry like {"wasa_data": {...}}
        # con_name = list(db_config.keys())[0]
        # config = db_config[con_name]

        # 2. Extract parameters from the config value
        engine = db_config.get("engine")
        host = db_config.get("host")
        port = db_config.get("port")
        user = db_config.get("user")
        password = db_config.get("password")
        db_name = db_config.get("name")

        # 3. Reuse your existing get_db_connection logic to handle
        # encryption and session management
        return cls.get_db_connection(
            con_name=cls.get_connection_name(db_name, host),
            engine=engine,
            host=host,
            port=port,
            user=user,
            password=password,
            db_name=db_name
        )

    @staticmethod
    def get_connection_name(db_name: str, host: str):
        return f"{db_name}_{host}"

    @classmethod
    def get_db_connection(cls, con_name: str, engine: str = None,
                          host: str = None, port: str = None,
                          user: str = None, password: str = None,
                          db_name: str = None) -> 'DBConnection':


        with cls.managed_session() as session:
            try:
                # Create a SQL statement to select the existing connection
                stmt = select(cls).where(cls.name == con_name)
                db_conn = session.execute(stmt).scalar_one_or_none()

                if db_conn is None:
                    # If no connection exists, validate all required parameters
                    if not all([engine, host, port, user, password, db_name]):
                        raise ValueError("All connection parameters must be provided to create a new database connection.")

                    # Encrypt the password
                    encrypted_password = CryptoUtils().encrypt_txt(password)
                    host = 'localhost' if os.environ.get("DATA_HOST") == host and IS_LOCAL == True else host
                    # Create a new DBConnection instance
                    db_conn = cls()
                    db_conn.name = con_name
                    db_conn.params = {
                        "engine": engine,
                        "host": host,
                        "port": port,
                        "user": user,
                        "password": encrypted_password,
                        "name": db_name
                    }
                    # Add the new connection to the session and commit
                    session.add(db_conn)
                    session.commit()

                return db_conn
            except SQLAlchemyError as e:
                session.rollback()
                raise e
            finally:
                # Close the session
                session.close()


    def get_sqlalchemy_engine(self) -> Engine:
        # self.params["password"] = CryptoUtils().decrypt_txt(self.params["password"])
        return ExternalEngineRegistry.get(self.name, self.params.copy())




class LayerQualityLevel(DBBase):
    __tablename__ = "dch_quality_level"
    id = mapped_column('id', db.INTEGER, primary_key=True, autoincrement=True)
    level = mapped_column('level', db.INTEGER, nullable=False)
    name = mapped_column('name', db.VARCHAR(50), nullable=False)
    description = mapped_column('description', db.TEXT, nullable=True)
    __pydantic__ = LayerQualityLevelType

class LayerAccessibility(DBBase):
    __tablename__ = "dch_layer_accessibility"

    id = Column(db.INTEGER, primary_key=True, autoincrement=True)
    accessibility_type = Column(
        ChoiceType(AccessibilityType.choices(), impl=db.VARCHAR(50)),
        nullable=False
    )
    params = Column(db.JSON, nullable=False)

    layer_info: Mapped["LayerInfo"] = relationship(
        back_populates="layer_accessibility"
    )

    __pydantic__ = LayerAccessibilityType


    @property
    def file_path(self) -> str | None:
        params = self.params or {}
        fp = params.get("file_path")
        if not fp:
            return None

        fp = str(fp).strip()
        if not fp:
            return None

        # keep S3/object-store paths unchanged
        if fp.startswith("s3://"):
            return fp

        # keep absolute local paths unchanged
        if os.path.isabs(fp):
            return fp

        is_media_rel = params.get("is_path_media_rel", False)
        if isinstance(is_media_rel, str):
            is_media_rel = is_media_rel.strip().lower() in {"true", "1", "yes", "y"}

        # join only when path is relative and explicitly marked media-relative
        if is_media_rel:
            return os.path.join(DCH_MEDIA_DIR, fp)

        # plain relative path, leave unchanged
        return fp

    @classmethod
    def create_db_accessibility(
        cls,
        table_name: str,
        db_conn: Union["DBConnection", str],
        columns: dict,
        pk_cols: list
    ):
        cols = []
        g_col = None

        for k, v in columns.items():
            v = str(v).lower()
            if "geometry" in v:
                g_col = k
            else:
                cols.append({
                    "name": k,
                    "d_type": ManageDataFrameUtils.getColGenericType(v)
                })

        conn_name = db_conn.name if hasattr(db_conn, "name") else db_conn
        params = {
            "conn_name": conn_name,
            "table_name": table_name,
            "columns": cols,
            "g_col": g_col,
            "pk_cols": pk_cols
        }

        with cls.managed_session() as session:
            stmt = select(cls).filter(
                cls.conn_name == conn_name,
                cls.table_name == table_name,
            )

            la = session.execute(stmt).scalars().first()

            if la is None:
                la = cls(
                    accessibility_type=AccessibilityType.DB.name,
                    params=params
                )
                session.add(la)
            else:
                la.accessibility_type = AccessibilityType.DB.name
                la.params = params

            session.commit()
            session.refresh(la)
            return la

    @hybrid_property
    def table_name(self) -> str | None:
        return (self.params or {}).get("table_name")

    @table_name.expression
    def table_name(cls):
        return cls.params.op("->>")("table_name")

    @hybrid_property
    def conn_name(self) -> str | None:
        return (self.params or {}).get("conn_name")

    @conn_name.expression
    def conn_name(cls):
        return cls.params.op("->>")("conn_name")

    @property
    def pk_cols(self):
        return (self.params or {}).get("pk_cols", [])

    @property
    def cols_name(self):
        cols = (self.params or {}).get("columns", [])
        return [c["name"] for c in cols if c.get("name")]

    @property
    def cols_type(self):
        cols = (self.params or {}).get("columns", [])
        return [c.get("d_type") for c in cols]

    def get_table_columns(
            self,
            session: Session,
            schema: str = "public"
    ) -> set[str]:
        if not self.table_name:
            return set()

        sql = text("""
                   SELECT column_name
                   FROM information_schema.columns
                   WHERE table_schema = :schema
                     AND table_name = :table_name
                   """)

        rows = session.execute(sql, {
            "schema": schema,
            "table_name": self.table_name
        }).fetchall()

        return {r[0] for r in rows}

    def get_valid_cols(
            self,
            session: Session,
            schema: str = "public"
    ) -> list[str]:
        """
        Returns only those metadata columns that really exist in the DB table.
        Prevents stale LayerAccessibility.params columns from breaking MVT/query generation.
        """
        db_cols = self.get_table_columns(session=session, schema=schema)
        metadata_cols = set(self.cols_name or [])

        valid_cols = metadata_cols.intersection(db_cols)
        missing_cols = metadata_cols - db_cols

        if missing_cols:
            da_logger.warning(
                f"[LayerColumnMismatch] table={self.table_name}, "
                f"missing={sorted(missing_cols)}"
            )

        return sorted(valid_cols)

    @property
    def geometry_col(self):
        return (self.params or {}).get("g_col")

    def get_external_engine(self):
        if (
            not self.accessibility_type
            or self.accessibility_type.code != AccessibilityType.DB.name
        ):
            return None

        conn_name = self.conn_name
        if not conn_name:
            return None

        db_conn = DBConnection.get_db_connection(con_name=conn_name)
        if not db_conn:
            return None

        return db_conn.get_sqlalchemy_engine()

    @classmethod
    def create_url_accessibility(cls, url: str, url_type: str):
        """
        Creates or updates a URL-based accessibility record.
        Utilizes the Mixin's managed_session and the safety-aware .save() method.
        """
        params = {"url_type": url_type, "url": url}

        # 1. Use managed_session to find if a record with this URL already exists
        with cls.managed_session() as session:
            # We query by the URL inside the JSON params
            stmt = select(cls).filter(
                cls.params.op("->>")("url") == url
            )
            existing_la = session.execute(stmt).scalars().first()

        # 2. If it exists, update it; otherwise, create a new instance
        la = existing_la if existing_la else cls()

        la.accessibility_type = AccessibilityType.URL.name
        la.params = params

        # 3. Use the Mixin's save() method.
        # Since we manually checked for existence to handle the logic,
        # we can pass allow_update=True because we know our intent.
        return la.save(allow_update=True)

    @hybrid_property
    def url(self) -> str | None:
        return (self.params or {}).get("url")

    @url.expression
    def url(cls):
        return cls.params.op("->>")("url")

    @hybrid_property
    def url_type(self) -> str | None:
        return (self.params or {}).get("url_type")

    @url_type.expression
    def url_type(cls):
        return cls.params.op("->>")("url_type")

class LayerInfo(DBBase):
    __tablename__ = "dch_layer_info"

    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    app_label = Column(db.VARCHAR(100), nullable=False, default="dch")
    title = Column(db.String, nullable=False)
    layer_name = Column(db.VARCHAR(100), nullable=False, unique=True)

    srid = Column(db.INTEGER, nullable=True)
    proj4 = Column(db.TEXT, nullable=True)

    extent = Column(db.ARRAY(item_type=db.Float()), nullable=True)
    extent_3857 = Column(db.ARRAY(item_type=db.Float()), nullable=True)

    # data_model = Column(SA_Enum(DataModel), nullable=False)
    data_model = Column(
        ChoiceType(DataModel.choices(), impl=db.VARCHAR(50)),
        nullable=False
    )
    # model_type = Column(SA_Enum(ModelType), nullable=False)
    model_type = Column(
        ChoiceType(ModelType.choices(), impl=db.VARCHAR(50)),
        nullable=False
    )

    geom_type = Column(db.ARRAY(db.VARCHAR(100)), nullable=True)
    layer_styling = Column(db.JSON, nullable=True)
    zoom_range = Column(db.ARRAY(item_type=db.Integer), nullable=False, default=[0, 30])

    uploaded_by = Column(db.Integer, nullable=True)
    uploaded_date = Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    temporal_res = Column(db.DATE, nullable=True)
    extra_metadata = Column(db.JSON, nullable=True)

    category_id = Column(
        db.ForeignKey(LayerCategory.id),
        name="category_id",
        nullable=True
    )
    layer_category = relationship("LayerCategory")

    layer_access_id = Column(
        db.ForeignKey(LayerAccessibility.id, ondelete="CASCADE"),
        name="layer_access_id",
        nullable=False
    )
    layer_accessibility: Mapped["LayerAccessibility"] = relationship(
        back_populates="layer_info",
        cascade="all, delete",
        passive_deletes=True
    )

    quality_level_id = Column(
        db.ForeignKey(LayerQualityLevel.id),
        name="quality_level_id",
        nullable=True
    )
    quality_level = relationship("LayerQualityLevel")

    data_url = Column(db.TEXT, nullable=True)
    layer_setting = Column(db.JSON, nullable=True)
    format = Column(db.VARCHAR(50), nullable=False, default="MVT")
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
    def get_layer_info_by_uuid(cls, uuid: str) -> Optional["LayerInfo"]:
        with cls.managed_session() as session:
            stmt = (
                select(cls)
                .options(
                    selectinload(cls.layer_accessibility),
                    selectinload(cls.layer_category),
                    selectinload(cls.quality_level),
                )
                .where(cls.uuid == uuid)
            )
            return session.execute(stmt).scalar_one_or_none()

    def to_layer_gdf(
            self,
            cols=(),
            g_col: str | None = None,
            extent_3857=None,
            where_clause: str | None = None,
            limit: int = -1,
            offset: int = 0,
            session: Session = None,
            min_intersection_ratio: float = 0.90,
    ) -> GeoDataFrame:
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
                pk_cols = access.pk_cols or []
                cols = list(pk_cols) + [
                    c for c in (access.cols_name or [])
                    if c and c.lower() not in {
                        "extent",
                        g_col.lower(),
                        "geojson",
                        "geom",
                        "geometry",
                    }
                ]

            seen = set()
            cols = [c for c in cols if c and not (c in seen or seen.add(c))]

            safe_table = DBQuery.safe_ident(access.table_name)
            safe_geom = DBQuery.safe_ident(g_col)

            if cols:
                safe_cols = ", ".join(DBQuery.safe_ident(c) for c in cols)
                sql = f"SELECT {safe_geom}, {safe_cols} FROM {safe_table}"
            else:
                sql = f"SELECT {safe_geom} FROM {safe_table}"

            where_clauses = []

            if where_clause:
                clean_where = where_clause.strip()
                if clean_where.lower().startswith("where"):
                    clean_where = clean_where[5:].strip()
                if clean_where:
                    where_clauses.append(f"({clean_where})")

            is_postgres = engine.dialect.name == "postgresql"

            if (
                    is_postgres
                    and extent_3857
                    and isinstance(extent_3857, (tuple, list))
                    and len(extent_3857) == 4
            ):
                minx, miny, maxx, maxy = [float(v) for v in extent_3857]
                ratio = max(0.0, min(float(min_intersection_ratio), 1.0))

                bbox_sql = f"""
                ST_Transform(
                    ST_SetSRID(
                        ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}),
                        3857
                    ),
                    {int(srid)}
                )
                """.strip()

                where_clauses.append(
                    f"""
                    ST_Intersects({safe_geom}, {bbox_sql})
                    AND
                    (
                        CASE
                            WHEN GeometryType({safe_geom}) IN ('LINESTRING', 'MULTILINESTRING')
                                 AND ST_Length({safe_geom}) > 0 THEN
                                ST_Length(ST_Intersection({safe_geom}, {bbox_sql}))
                                / ST_Length({safe_geom}) >= {ratio}

                            WHEN GeometryType({safe_geom}) IN ('POLYGON', 'MULTIPOLYGON')
                                 AND ST_Area({safe_geom}) > 0 THEN
                                ST_Area(ST_Intersection({safe_geom}, {bbox_sql}))
                                / ST_Area({safe_geom}) >= {ratio}

                            ELSE
                                ST_Intersects({safe_geom}, {bbox_sql})
                        END
                    )
                    """.strip()
                )

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            if limit is not None and limit >= 0:
                sql += f" LIMIT {int(limit)}"

            if offset and offset > 0:
                sql += f" OFFSET {int(offset)}"

            with Session(engine) as session:
                gdf = DBQuery.execute_query_as_gdf(
                    session,
                    sql,
                    geom_col=g_col,
                    crs=f"EPSG:{srid}"
                )

            return gdf

        except Exception as e:
            da_logger.exception(f"Failed to load layer GDF for {self.layer_name}: {e}")
            return GeoDataFrame()

    def count_layer_rows(
            self,
            where_clause: str | None = None,
            extent_3857=None,
            g_col: str | None = None,
    ) -> int:
        """
        Return row/feature count for a vector DB layer.
        Supports optional where_clause and bbox filtering.
        """
        try:
            if not self.data_model or self.data_model.code != DataModel.V.name:
                return 0

            access = self.layer_accessibility
            if not access or not access.accessibility_type:
                return 0

            if access.accessibility_type.code != AccessibilityType.DB.name:
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

            if (
                    is_postgres
                    and g_col
                    and extent_3857
                    and isinstance(extent_3857, (tuple, list))
                    and len(extent_3857) == 4
            ):
                minx, miny, maxx, maxy = extent_3857
                safe_geom = DBQuery.safe_ident(g_col)
                where_clauses.append(
                    f"""
                    ST_Intersects(
                        {safe_geom},
                        ST_Transform(
                            ST_SetSRID(ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}), 3857),
                            {srid}
                        )
                    )
                    """.strip()
                )

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            with Session(engine) as session:
                result = session.execute(text(sql)).scalar_one()

            return int(result or 0)

        except Exception as e:
            da_logger.exception(f"Error counting rows for layer {self.layer_name}: {e}")
            return 0

    def to_layer_df(
            self,
            cols=(),
            where_clause: str | None = None,
            limit: int = -1,
            offset: int = 0,
    ) -> pd.DataFrame:
        """
        Returns a DataFrame for:
        - Vector layers backed by DB tables
        - Raster layers backed by file access

        Parameters
        ----------
        cols : iterable
            Columns to select for vector layers.
        where_clause : str | None
            Optional SQL WHERE clause.
            Can be:
                "dist_id = 1"
            or:
                "WHERE dist_id = 1"
        limit : int
            -1 means no LIMIT (fetch all rows)
        offset : int
            SQL OFFSET value
        """
        try:
            if not self.data_model:
                return pd.DataFrame()

            # -------------------------------------------------
            # Vector layer backed by DB
            # -------------------------------------------------
            if self.data_model.code == DataModel.V.name:
                access = self.layer_accessibility
                if not access or not access.accessibility_type:
                    return pd.DataFrame()

                if access.accessibility_type.code != AccessibilityType.DB.name:
                    return pd.DataFrame()

                engine = access.get_external_engine()
                if engine is None or not access.table_name:
                    return pd.DataFrame()

                # Default selected columns
                if not cols:
                    pk_cols = access.pk_cols or []
                    cols = list(pk_cols) + [
                        info["name"]
                        for info in ((access.params or {}).get("columns") or [])
                        if info.get("name")
                           and info["name"].lower() not in {
                               "extent",
                               (access.geometry_col or "").lower(),
                               "geojson",
                               "geom",
                               "geometry",
                           }
                    ]

                # De-duplicate while preserving order
                seen = set()
                cols = [c for c in cols if c and not (c in seen or seen.add(c))]

                if not cols:
                    return pd.DataFrame()

                safe_table = DBQuery.safe_ident(access.table_name)
                select_cols = ", ".join(DBQuery.safe_ident(c) for c in cols)
                sql = f"SELECT {select_cols} FROM {safe_table}"

                if where_clause:
                    clean_where = where_clause.strip()
                    sql += (
                        f" {clean_where}"
                        if clean_where.lower().startswith("where")
                        else f" WHERE {clean_where}"
                    )

                if limit is not None and limit >= 0:
                    sql += f" LIMIT {int(limit)}"

                if offset and offset > 0:
                    sql += f" OFFSET {int(offset)}"

                with Session(engine) as session:
                    return DBQuery.execute_stmt_as_df(session, sql)

            # -------------------------------------------------
            # Raster layer backed by file
            # -------------------------------------------------
            if self.data_model.code == DataModel.R.name:
                raster = self.to_raster()
                if raster is None:
                    return pd.DataFrame()

                band_count = raster.get_spectral_resolution()

                if band_count == 1:
                    res = BandProcess.get_value_count_data(
                        raster.get_data_array(1),
                        raster.get_nodata_value()
                    )
                    return pd.DataFrame.from_records(res)

                summaries = []
                for band in range(1, band_count + 1):
                    summaries.append(
                        BandProcess.get_summary_data(
                            raster.get_data_array(band),
                            nodata=raster.get_nodata_value()
                        )
                    )
                return pd.DataFrame(summaries)

            return pd.DataFrame()

        except Exception as e:
            da_logger.exception(f"Error converting Layer to DataFrame: {e}")
            return pd.DataFrame()

    def to_raster(self) -> Optional[RioRaster]:
        """
        Return RioRaster for file-based raster layers.
        Uses LayerAccessibility.file_path semantics.
        """
        try:
            if not self.data_model or self.data_model.code != DataModel.R.name:
                return None

            access = self.layer_accessibility
            if not access or not access.accessibility_type:
                return None

            if access.accessibility_type.code != AccessibilityType.File.name:
                return None

            file_path = access.file_path
            if not file_path or not os.path.exists(file_path):
                return None

            return RioRaster(file_path)

        except Exception as e:
            da_logger.exception(f"Error opening raster for {self.layer_name}: {e}")
            return None

    def to_cog(self) -> Optional[COGRaster]:
        """
        Return COGRaster for file-based raster layers.
        """
        try:
            if not self.data_model or self.data_model.code != DataModel.R.name:
                return None

            access = self.layer_accessibility
            if not access or not access.accessibility_type:
                return None

            if access.accessibility_type.code != AccessibilityType.File.name:
                return None

            file_path = access.file_path
            if not file_path or not os.path.exists(file_path):
                return None

            return COGRaster.open_from_local(str(file_path))

        except Exception as e:
            da_logger.exception(f"Error opening COG for {self.layer_name}: {e}")
            return None

    def get_row_detail(self, col_name, col_value):
        """
        Return row details for a vector layer by a validated column/value pair.
        """
        try:
            if not self.data_model or self.data_model.code != DataModel.V.name:
                return []

            access = self.layer_accessibility
            if not access or not access.accessibility_type:
                return []

            if access.accessibility_type.code != AccessibilityType.DB.name:
                return []

            if not access.table_name:
                return []

            allowed_cols = set(access.cols_name or [])
            if col_name not in allowed_cols:
                return []

            engine = access.get_external_engine()
            if engine is None:
                return []

            table = DBQuery.safe_ident(access.table_name)
            safe_col = DBQuery.safe_ident(col_name)

            sql = db.text(
                f"SELECT * FROM {table} WHERE {safe_col} = :col_value LIMIT 50"
            )

            with Session(engine) as session:
                return session.execute(sql, {"col_value": col_value}).mappings().all()

        except Exception as e:
            da_logger.exception(f"Error fetching row detail for {self.layer_name}: {e}")
            return []


    def update_extent(self, new_bounds: list, overwrite: bool = False):
        """
        Updates self.extent (native) and self.extent_3857 (Web Mercator).
        new_bounds: [minx, miny, maxx, maxy] in the layer's native SRID.
        """
        with self.managed_session() as session:
            session.add(self)

            # 1. Prepare New Bounds as Floats
            new_b = [float(x) for x in new_bounds]

            # 2. Update Native Extent (self.extent)
            if overwrite or not self.extent or len(self.extent) != 4:
                self.extent = new_b
            else:
                self.extent = [
                    min(self.extent[0], new_b[0]),
                    min(self.extent[1], new_b[1]),
                    max(self.extent[2], new_b[2]),
                    max(self.extent[3], new_b[3])
                ]

            # 3. Handle EPSG:3857 Extent (self.extent_3857)
            # Transform the newly calculated native extent to 3857
            try:
                native_srid = self.srid or 3857
                if native_srid == 3857:
                    self.extent_3857 = self.extent
                else:
                    # Create transformer from native SRID to 3857
                    transformer = Transformer.from_crs(
                        f"EPSG:{native_srid}",
                        "EPSG:3857",
                        always_xy=True
                    )
                    # Transform corners: (minx, miny) and (maxx, maxy)
                    x1, y1 = transformer.transform(self.extent[0], self.extent[1])
                    x2, y2 = transformer.transform(self.extent[2], self.extent[3])

                    # Note: If transforming from 4326 to 3857, ensure coordinates
                    # don't exceed Web Mercator limits (+/- 85.06 lat)
                    self.extent_3857 = [x1, y1, x2, y2]

            except Exception as tf_err:
                da_logger.error(f"Failed to transform extent to 3857 for {self.layer_name}: {tf_err}")

            # 4. Finalize
            flag_modified(self, "extent")
            flag_modified(self, "extent_3857")
            session.commit()
            da_logger.info(f"Extent and Extent_3857 updated for {self.layer_name} (Overwrite: {overwrite})")

class MapInfo(DBBase):
    __tablename__ = "dch_map_info"
    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    # name = Column(db.VARCHAR(100), unique=True, nullable=False)
    title = Column(db.VARCHAR(100), nullable=False, unique=True)
    extent = Column(db.ARRAY(item_type=db.Float), nullable=False)
    layers = Column(db.JSON, nullable=False)
    srid = Column(db.INTEGER, nullable=False, default=3857)
    units = Column(db.VARCHAR(10), nullable=False, default='m')
    description = Column(db.TEXT, nullable=True)
    navigation_setting = Column(db.JSON, nullable=True)
    __pydantic__ = MapInfoType

    @classmethod
    def get_all_map_info(cls, limit=None):
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
            con.close()
            return df

    def get_layer_info(self, uuid):
        for layer in self.layers:
            if layer["uuid"] == uuid:
                return layer
        return None
