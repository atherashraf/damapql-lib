import os
from typing import Union, Optional
from urllib.parse import quote_plus

import pandas as pd
import sqlalchemy as db
from digitalarzengine.io.cog_raster import COGRaster


from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from geopandas import GeoDataFrame
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.exc import SQLAlchemyError


from sqlalchemy import Column, func, select, create_engine, Engine, Enum as SA_Enum
from sqlalchemy.orm import mapped_column, Mapped, relationship, Session
from sqlalchemy_utils import ChoiceType
from app.dch.api_enum import AccessibilityType, DataModel, ModelType
from app.dch.dtypes import LayerCategoryType, DBConnectionType, LayerAccessibilityType, LayerQualityLevelType, \
    LayerInfoType, MapInfoType
from app.dch.utils.df_utils import ManageDataFrameUtils
from utils.adapters.db import DBBase
from utils.adapters.deps import ExternalEngineRegistry
from utils.adapters.services.db_query import DBQuery
from utils.crypto import CryptoUtils
from utils.singletons import da_logger
from settings import COG_DIR, IS_LOCAL, MEDIA_DIR
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
        return ExternalEngineRegistry.get(self.name, self.params.copy())

class LayerAccessibility(DBBase):
    __tablename__ = "dch_layer_accessibility"
    id = Column(db.INTEGER, primary_key=True, autoincrement=True)
    accessibility_type = Column(ChoiceType(AccessibilityType.choices(), impl=SA_Enum(AccessibilityType)), nullable=True)
    params = Column(db.JSON, nullable=False)
    layer_info: Mapped["LayerInfo"] = relationship(back_populates="layer_accessibility")

    __pydantic__ = LayerAccessibilityType



    @property
    def file_path(self):
        fp = self.params['file_path']
        # fp = fp if "s3://" in fp else os.path.join(COG_DIR, fp)
        if self.params['is_path_media_rel']:
            fp = os.path.join(MEDIA_DIR, fp)
        return fp

    @classmethod
    def create_db_accessibility(cls, table_name: str, db_conn: Union[DBConnection, str], columns: dict, pk_cols: list):
        cols = []
        g_col = None

        for k, v in columns.items():
            v = str(v)
            if v != "geometry":
                cols.append({"name": k, "d_type": ManageDataFrameUtils.getColGenericType(v)})
            else:
                g_col = k

        conn_name = db_conn.name if isinstance(db_conn, DBConnection) else db_conn
        params = {
            "conn_name": conn_name,
            "table_name": table_name,
            "columns": cols,
            "g_col": g_col,
            "pk_cols": pk_cols
        }

        with cls.managed_session() as session:
            try:
                qs = select(LayerAccessibility).filter(
                    LayerAccessibility.params.op("->>")("conn_name").cast(db.String) == conn_name,
                    LayerAccessibility.params.op("->>")("table_name").cast(db.String) == table_name
                )
                result = session.execute(qs).scalars().first()
                if result is None:
                    la = LayerAccessibility()
                    la.accessibility_type = AccessibilityType.DB.name
                    la.params = params
                    session.add(la)  # ✅ Only add if new

                else:
                    la = result
                    la.accessibility_type = AccessibilityType.DB.name
                    la.params = params

                session.commit()
                session.refresh(la)
                return la
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    @property
    def table_name(self):
        return self.params["table_name"] if "table_name" in self.params else None

    @property
    def pk_cols(self):
        return self.params["pk_cols"] if "pk_cols" in self.params else []

    @property
    def cols_name(self):
        cols = self.params["columns"] if "columns" in self.params else []
        return [c['name'] for c in cols]

    @property
    def cols_type(self):
        cols = self.params["columns"] if "columns" in self.params else []
        return [c['type'] for c in cols]

    @property
    def geometry_col(self):
        return self.params["g_col"] if "g_col" in self.params else None

    @classmethod
    def create_url_accessibility(cls, url: str, url_type: str) -> "LayerAccessibility":
        params = {"url_type": url_type, "url": url}

        stmt = select(LayerAccessibility).where(
            LayerAccessibility.params.op("->>")("url") == url
        )

        with cls.managed_session() as session:
            # Fetch existing as ORM object
            existing = session.execute(stmt).scalars().first()

            la = existing if existing else LayerAccessibility()
            la.accessibility_type = AccessibilityType.URL.name
            la.params = params

            # IMPORTANT: save using same session (assuming DBBase.save(session=...) exists)
            la.save(session=session)

            return la

class LayerQualityLevel(DBBase):
    __tablename__ = "dch_quality_level"
    id = mapped_column('id', db.INTEGER, primary_key=True, autoincrement=True)
    level = mapped_column('level', db.INTEGER, nullable=False)
    name = mapped_column('name', db.VARCHAR(50), nullable=False)
    description = mapped_column('description', db.TEXT, nullable=True)
    __pydantic__ = LayerQualityLevelType


class LayerInfo(DBBase):
    __tablename__ = "dch_layer_info"
    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    app_label = Column(db.VARCHAR(100), nullable=False, default="dch")
    title = Column(db.String, nullable=False)
    layer_name = Column(db.VARCHAR(100), nullable=False)
    srid = Column(db.INTEGER, nullable=True)
    proj4 = Column(db.TEXT, nullable=True)
    extent = Column(db.ARRAY(item_type=db.Float()), nullable=True)
    extent_3857 = Column(db.ARRAY(item_type=db.Float()), nullable=True)
    # data_model = Column(ChoiceType(DataModel.choices(),impl=VARCHAR(50)), nullable=False)
    data_model = Column(ChoiceType(DataModel.choices(), impl=SA_Enum(DataModel)), nullable=False)
    model_type = Column(ChoiceType(ModelType.choices(), impl=SA_Enum(ModelType)), nullable=True)
    # geom_type = Column(db.ARRAY(ChoiceType(GeomType.choices())), nullable=True)
    geom_type = Column(db.ARRAY(db.VARCHAR(100)), nullable=True)
    layer_styling = Column(db.JSON, nullable=True)
    zoom_range = Column(db.ARRAY(item_type=db.Integer), nullable=False, default=[0, 30])
    uploaded_by = Column(db.Integer, nullable=True)
    uploaded_date = Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    temporal_res = Column(db.DATE, nullable=True)
    extra_metadata = Column(db.JSON, nullable=True)
    category_id = Column(db.ForeignKey(LayerCategory.id), name='category_id', nullable=True)
    layer_category = relationship('LayerCategory')

    layer_access_id = Column(db.ForeignKey(LayerAccessibility.id, ondelete="CASCADE"),
        name='layer_access_id',nullable=False
    )
    layer_accessibility: Mapped[LayerAccessibility] = relationship(
        back_populates="layer_info", cascade="all, delete", passive_deletes=True
    )

    quality_level_id = Column(db.ForeignKey(LayerQualityLevel.id), name='quality_level_id',
                              nullable=True)
    quality_level = relationship('LayerQualityLevel')

    data_url = Column(db.TEXT, nullable=True)
    layer_setting = Column(db.JSON, nullable=True)
    format = Column(db.VARCHAR(50), nullable=False, default="MVT")
    date_range_url = Column(db.TEXT, nullable=True)
    __pydantic__ = LayerInfoType

    @property
    def pk_concat_char(self):
        return "$"

    @property
    def pk_cols_as_pk(self) -> str:
        pk_cols = self.layer_accessibility.pk_cols
        pk_col = ""
        for col in pk_cols:
            pk_col += f"{col}" if pk_col == "" else f" || '{self.pk_concat_char}' || {col}"
        pk_col += " as pk"
        return pk_col

    @classmethod
    def get_layer_info_by_uuid(cls, uuid):
        stmt = select(cls).where(uuid=uuid)
        with cls.managed_session() as session:
            res = session.execute(stmt).fetchone()
            session.close()
            return res

    from sqlalchemy.orm import Session
    import geopandas as gpd

    def to_layer_gdf(self, g_col=None, extent_3857=None, where_clause=None) -> GeoDataFrame:
        """
        Queries external DB and returns GeoDataFrame.
        - No manager usage.
        - Uses DBConnection + SQLAlchemy Session + DBQuery.
        """
        if self.data_model.code != DataModel.V.name:
            return GeoDataFrame()

        access = self.layer_accessibility
        if access.accessibility_type.code != AccessibilityType.DB.name:
            return GeoDataFrame()

        try:
            params = access.params

            # 1) get external engine from DBConnection record
            db_conn = DBConnection.get_db_connection(con_name=params["conn_name"])
            engine = db_conn.get_sqlalchemy_engine()

            # 2) geometry column + srid
            g_col = g_col or access.geometry_col
            srid = self.srid or 4326

            # 3) build column list (avoid duplicating geom)
            cols = set(access.pk_cols or [])
            cols.update(
                c for c in (access.cols_name or [])
                if c and c.lower() not in {"extent", g_col.lower(), "geojson", "geom"}
            )

            select_cols = ", ".join(f'"{c}"' for c in cols) if cols else ""
            if select_cols:
                sql = f'SELECT "{g_col}", {select_cols} FROM {access.table_name}'
            else:
                sql = f'SELECT "{g_col}" FROM {access.table_name}'

            # 4) WHERE clauses
            where_clauses = []
            if where_clause:
                where_clauses.append(f"({where_clause})")

            is_postgres = engine.dialect.name == "postgresql"

            # bbox filter (extent_3857 is in EPSG:3857)
            if is_postgres and extent_3857 and isinstance(extent_3857, (tuple, list)) and len(extent_3857) == 4:
                minx, miny, maxx, maxy = extent_3857
                where_clauses.append(
                    f'''ST_Intersects(
                            "{g_col}",
                            ST_Transform(
                                ST_SetSRID(ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}), 3857),
                                {srid}
                            )
                        )'''
                )

            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            # 5) execute -> gdf
            with Session(engine) as session:
                gdf = DBQuery.execute_query_as_gdf(
                    session,
                    sql,
                    geom_col=g_col,
                    crs=f"EPSG:{srid}"
                )

            # 6) fallback manual spatial filter for non-postgres
            if (not is_postgres) and extent_3857 and not gdf.empty:
                from shapely.geometry import box
                bbox = box(*extent_3857)

                # IMPORTANT: bbox is in 3857; reproject gdf to 3857 before intersect, then return to original crs
                src_crs = gdf.crs
                gdf_3857 = gdf.to_crs(epsg=3857)
                gdf_3857 = gdf_3857[gdf_3857.geometry.intersects(bbox)]
                gdf = gdf_3857.to_crs(src_crs) if src_crs else gdf_3857

            return gdf

        except Exception as e:
            da_logger.exception(f"Failed to load layer GDF for {self.layer_name}: {e}")
            return GeoDataFrame()

    def to_layer_df(self, cols=(), where_clause=None) -> pd.DataFrame:
        """
        Returns a DataFrame for Vector (DB) or Raster (file).
        - No manager usage.
        """
        try:
            # ---------------------------
            # Vector (DB table)
            # ---------------------------
            if self.data_model.code == DataModel.V.name:
                access = self.layer_accessibility
                if access.accessibility_type.code != AccessibilityType.DB.name:
                    return pd.DataFrame()

                params = access.params

                db_conn = DBConnection.get_db_connection(con_name=params["conn_name"])
                engine = db_conn.get_sqlalchemy_engine()

                # build columns
                if not cols:
                    pk_cols = access.pk_cols or []
                    cols = set(pk_cols + [
                        info["name"] for info in (params.get("columns") or [])
                        if info.get("name")
                           and info["name"].lower() not in {"extent", params.get("g_col", "").lower(), "geojson",
                                                            "geom"}
                    ])

                select_cols = ", ".join(f'"{c}"' for c in cols)
                sql = f'SELECT {select_cols} FROM {params["table_name"]}'
                if where_clause:
                    # if caller passes "WHERE ..." keep as-is; otherwise prefix WHERE
                    sql += f" {where_clause}" if where_clause.strip().lower().startswith(
                        "where") else f" WHERE {where_clause}"

                with Session(engine) as session:
                    return DBQuery.execute_stmt_as_df(session, sql)

            # ---------------------------
            # Raster (file)
            # ---------------------------
            if self.data_model.code == DataModel.R.name:
                access = self.layer_accessibility
                if access.accessibility_type.code != AccessibilityType.File.name:
                    return pd.DataFrame()

                path = access.params["file_path"]
                if access.params.get("is_path_media_rel"):
                    path = os.path.join(MEDIA_DIR, path)
                else:
                    # if stored relative to COG_DIR in non-s3 mode
                    if not path.startswith("s3://") and not os.path.isabs(path):
                        path = os.path.join(COG_DIR, path)

                raster = RioRaster(path)
                band_count = raster.get_spectral_resolution()

                if band_count == 1:
                    res = BandProcess.get_value_count_data(
                        raster.get_data_array(1),
                        raster.get_nodata_value()
                    )
                    return pd.DataFrame.from_records(res)

                summaries = []
                for band in range(1, band_count + 1):  # ✅ your old loop was 0..band_count-1 but rio is 1-based
                    summaries.append(BandProcess.get_summary_data(
                        raster.get_data_array(band),
                        nodata=raster.get_nodata_value()
                    ))
                return pd.DataFrame(summaries)

        except Exception as e:
            da_logger.exception(f"Error converting Layer to DataFrame: {e}")
            return pd.DataFrame()

    def to_raster(self) -> Optional[RioRaster]:
        if self.data_model.code == DataModel.R.name:
            if self.layer_accessibility.accessibility_type.code == AccessibilityType.File.name:
                file_path = self.layer_accessibility.params["file_path"]
                if self.layer_accessibility.params['is_path_media_rel'] == 'true':
                    file_path = os.path.join(MEDIA_DIR, file_path)
                return RioRaster(file_path)
        return None

    def to_cog(self) -> Optional[COGRaster]:
        if self.data_model.code == DataModel.R.name:
            if self.layer_accessibility.accessibility_type.code == AccessibilityType.File.name:
                file_path = self.layer_accessibility.params["file_path"]
                # if "s3://" in file_path:
                #     s3_utils = S3Manager(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION_NAME)
                #     cog_raster = COGRaster.open_from_s3(file_path, s3_utils.get_session())
                # else:
                file_path = os.path.join(COG_DIR)
                cog_raster = COGRaster.open_from_local(str(file_path))
                return cog_raster
        return None

    def get_row_detail(self, col_name, col_value):
        params = self.layer_accessibility.params
        connection: DBConnection = DBConnection.query().filter_by(name=params["conn_name"]) \
            .first()
        engine = connection.get_sqlalchemy_engine()
        if 'postgresql' in str(engine.url.drivername):
            query = f"SELECT * " \
                    f"from {params['table_name']} " \
                    f"where {col_name} = '{col_value}'"
            # df = pd.read_sql(query, con=engine)
            df = connection.manager(engine).execute_stmt_as_df(query)
            return df.to_dict(orient='records')
        return None



    def get_geometry(self, where_clause: str, params: dict | None = None) -> str | None:
        """
        Return single geometry WKT in EPSG:3857 from external DB.

        where_clause: SQL fragment for WHERE (e.g. '"id" = :id')
        params: bind params dict (e.g. {"id": 10})
        """
        if self.data_model.code != DataModel.V.name:
            return None

        la = self.layer_accessibility
        if la.accessibility_type.code != AccessibilityType.DB.name:
            return None

        p = la.params
        conn_name = p.get("conn_name")
        table_name = p.get("table_name")
        g_col = p.get("g_col")

        if not (conn_name and table_name and g_col and where_clause):
            return None

        # 1) Read connection record from MAIN DB using managed_session
        with DBConnection.managed_session() as session:
            conn_obj = session.execute(
                select(DBConnection).where(DBConnection.name == conn_name)
            ).scalar_one_or_none()

        if conn_obj is None:
            return None

        # 2) Cached external engine
        engine = ExternalEngineRegistry.get(key=conn_name, conn=conn_obj.params)

        # 3) Safe identifiers (table/column)
        t = DBQuery.safe_ident(table_name)
        g = DBQuery.safe_ident(g_col)

        sql = f"""
            SELECT ST_AsText(ST_Transform({g}, 3857)) AS geom
            FROM {t}
            WHERE {where_clause}
            LIMIT 1
        """

        # 4) Query external DB
        with Session(engine) as ext_session:
            row = DBQuery.execute_query_as_one(ext_session, sql, params=params or {})

        return row.get("geom") if row else None




class MapInfo(DBBase):
    __tablename__ = "dch_map_info"
    id = Column(db.BIGINT, primary_key=True, autoincrement=True)
    uuid = Column(db.VARCHAR(40), nullable=False, unique=True)
    title = Column(db.VARCHAR(100), nullable=False)
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
        with cls.manager().engine.connect() as con:
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
