from contextlib import contextmanager

import pandas as pd
import geopandas as gpd
from typing import Union, Any, Tuple, Optional, List, Dict, Generator

from sqlalchemy import Select, func, inspect as sa_inspect, ColumnElement, Engine, text
from sqlalchemy.orm import Session, Query, InstrumentedAttribute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from geoalchemy2 import Geometry, WKBElement
from geoalchemy2.shape import to_shape
from shapely import wkb
from datetime import date, datetime

from settings import DATABASES, DA_APPS, DEFAULT_DB_KEY
from utils.adapters.deps import get_sync_engine_instance, sync_session_scope, _sync_session_factories


class AmbiguousGeometryError(Exception):
    """Raised when a model has multiple geometry columns and none is specified."""
    pass


class ModelHelpersMixin:
    class _Meta:
        is_spatial = True
        db_key = None
        app_label = None

    # ----- internal Service Locator ----

    @classmethod
    def get_engine(cls, app_label: str = None) -> Engine:
        """Returns the Sync Engine for this model's database key."""
        db_key = cls.get_db_key(app_label)
        return get_sync_engine_instance(db_key)

    @classmethod
    def get_session(cls) -> Session:
        """
        Returns a new standalone session based on the model's db_key.
        Note: The caller is responsible for closing this session!
        """
        # 1. Ensure the engine and factory are initialized for this key
        db_key = cls.get_db_key()
        get_sync_engine_instance(db_key)

        # 2. Grab the session factory from the internal registry
        SessionLocal = _sync_session_factories[db_key]

        # 3. Return a fresh session instance
        return SessionLocal()

    @classmethod
    @contextmanager
    def managed_session(cls, session: Session = None, app_label: str = None) -> Generator[Session, None, None]:
        """The single source of truth for session management in the Mixin."""
        db_key = cls.get_db_key(app_label)
        if session:
            yield session
        else:
            with sync_session_scope(db_key) as s:
                yield s

    @classmethod
    def get_db_key(cls, app_label: str = None) -> str:
        """
        Determines the database key for this model.
        Priority: 1. _Meta.db_key, 2. _Meta.app_label, 3. Module path.
        """
        if hasattr(cls._Meta, "db_key") and cls._Meta.db_key is not None:
            return cls._Meta.db_key

        if app_label is None:
            if hasattr(cls._Meta, "app_label") and cls._Meta.app_label is not None:
                app_label = getattr(cls._Meta, "app_label")
            else:
                # Fallback: take the parent directory name of the model file
                app_label = cls.__module__.split(".")[-2]

        return DA_APPS.get(app_label, {}).get("db_key") or DEFAULT_DB_KEY

    @classmethod
    def get_db_params(cls, app_label: str = None) -> Dict[str, Any]:
        """
        Retrieves the configuration dictionary for the resolved database key.
        """
        key = cls.get_db_key(app_label)
        # Safely get the config, falling back to 'default' if the key doesn't exist
        return DATABASES.get(key, DATABASES.get(DEFAULT_DB_KEY))

    @classmethod
    def get_engine(cls, app_label: str = None) -> Engine:
        """
        Returns the SQLAlchemy Engine instance for the resolved database key.
        """
        # 1. Resolve which DB key we are using (e.g., 'cache_db' or 'default')
        db_key = cls.get_db_key(app_label)

        # 2. Use your existing utility to get/create the engine instance
        # This assumes get_sync_engine_instance is already set up to
        # look at your DATABASES dict and create an engine.
        return get_sync_engine_instance(db_key)



    # --- HYBRID SAVE & DELETE ---
    @classmethod
    def bulk_save(
            cls,
            data: Union[List[Union[dict, Any]], pd.DataFrame, gpd.GeoDataFrame],
            unique_fields: List[str] = None,
            app_label: str = None
    ):
        """
        High-speed bulk upsert.
        Uses app_label to resolve the correct database engine, then applies
        dialect-specific 'ON CONFLICT' logic.
        """
        if data is None or (isinstance(data, (pd.DataFrame, list)) and len(data) == 0):
            return

        # 1. Prepare Data
        if isinstance(data, (pd.DataFrame, gpd.GeoDataFrame)):
            if isinstance(data, gpd.GeoDataFrame):
                # Ensure geometry is WKT for PostGIS bulk compatibility
                temp_df = data.copy()
                geom_col = temp_df.geometry.name
                temp_df[geom_col] = temp_df[geom_col].apply(lambda x: x.wkt if x and hasattr(x, 'wkt') else None)
                processed_data = temp_df.to_dict('records')
            else:
                processed_data = data.to_dict('records')
        else:
            processed_data = [
                (item.model_dump() if hasattr(item, 'model_dump') else item)
                for item in data
            ]

        # 2. RESOLVE ENGINE VIA PARAMETERS
        # We use the provided app_label or fall back to the model's default routing
        engine = cls.get_engine(app_label=app_label)
        dialect = engine.dialect.name
        table = cls.__table__


        # 3. Dialect-Specific Statement Construction
        if dialect == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(table).values(processed_data)

            if unique_fields:
                update_dict = {
                    c.name: stmt.excluded[c.name]
                    for c in table.columns
                    if c.name not in unique_fields and not c.primary_key
                }
                if update_dict:
                    stmt = stmt.on_conflict_do_update(index_elements=unique_fields, set_=update_dict)
                else:
                    stmt = stmt.on_conflict_do_nothing(index_elements=unique_fields)

        elif dialect == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = sqlite_insert(table).values(processed_data)
            if unique_fields:
                update_dict = {
                    c.name: stmt.excluded[c.name]
                    for c in table.columns if c.name not in unique_fields
                }
                stmt = stmt.on_conflict_do_update(index_elements=unique_fields, set_=update_dict)

        else:
            from sqlalchemy import insert as base_insert
            stmt = base_insert(table).values(processed_data)

        # 4. Execute via the resolved engine
        # with engine.begin() as conn:
        #     conn.execute(stmt)

        with engine.connect() as conn:
            with conn.begin():
                conn.execute(stmt)

    # ... inside ModelHelpersMixin ...

    def save(self, session: Union[Session, AsyncSession] = None, app_label: str = None,
             unique_fields: List[str] = None, allow_update: bool = False) -> Any:
        # 1. Resolve defaults
        if unique_fields is None:
            unique_fields = getattr(self._Meta, "unique_fields", [])

        with self.managed_session(session, app_label) as s:
            existing = None

            # 2. Check Uniqueness
            if unique_fields:
                criteria = {f: getattr(self, f) for f in unique_fields if hasattr(self.__class__, f)}
                existing = s.query(self.__class__).filter_by(**criteria).first()

            if not existing:
                pk_fields = self.get_primary_key_fields()
                if pk_fields:
                    pk_val = getattr(self, pk_fields[0], None)
                    if pk_val: existing = s.get(self.__class__, pk_val)

            # 3. Handle Conflicts
            if existing:
                if not allow_update:
                    conflict_vals = {f: getattr(self, f) for f in unique_fields} or {"PK": pk_val}
                    raise ValueError(
                        f"CONFLICT: {self.__class__.__name__} {conflict_vals} exists. Use allow_update=True to overwrite.")

                for attr in self.__table__.columns.keys():
                    if attr not in self.get_primary_key_fields():
                        setattr(existing, attr, getattr(self, attr))
                target, is_add = existing, False
            else:
                target, is_add = self, True

            if isinstance(s, AsyncSession):
                return self._save_async(target, s, is_add)
            return self._save_sync(target, s, is_add)

    @staticmethod
    def _save_sync(obj, session, is_add):
        if is_add: session.add(obj)
        session.commit()
        session.flush()
        session.refresh(obj)
        return obj.to_object()

    @staticmethod
    async def _save_async(obj, session, is_add):
        if is_add: session.add(obj)
        await session.commit()
        await session.flush()
        await session.refresh(obj)
        return obj.to_object()

    def delete(self, session: Union[Session, AsyncSession] = None, app_label=None):
        with self.managed_session(session, app_label) as s:
            if isinstance(s, AsyncSession):
                return self._delete_async(self, s)
            s.delete(self)
            s.commit()
            return None

    @staticmethod
    async def _delete_async(obj, session):  # Fixed: removed extra 'self'
        await session.delete(obj)
        await session.commit()

    # --- ATTRIBUTE AND GEOMETRY HANDLING & SPATIAL UTILS ---
    @classmethod
    def get_geom_cols(cls) -> dict:
        if cls._Meta.is_spatial:
            return {name: col for name, col in cls.__table__.columns.items()
                    if isinstance(col.type, Geometry)}
        return {}

    @classmethod
    def get_geom_col_name_srid(cls) -> Tuple[Optional[str], Optional[int]]:
        cols = cls.get_geom_cols()
        if cols:
            name = list(cols.keys())[0]
            return name, cols[name].type.srid
        return None, None

    @staticmethod
    def convert_wkb(wkb_element: Any):
        if isinstance(wkb_element, WKBElement):
            return wkb.loads(bytes(wkb_element.data), hex=True)
        return wkb_element

    @classmethod
    def get_primary_key_fields(cls) -> list[str]:
        return [col.name for col in sa_inspect(cls).primary_key]

    @classmethod
    def get_fields(cls, skip_fields=None):
        if skip_fields is None:
            skip_fields = []

        # Use SQLAlchemy's mapper to get actual column names
        # This avoids picking up methods, internal SA state, or helper properties
        mapper = sa_inspect(cls)
        return [
            column.key for column in mapper.attrs
            if column.key not in skip_fields
        ]

    @staticmethod
    def sa_type_to_js_type(col: ColumnElement[Any]) -> str:
        """
        Map SQLAlchemy column types to simple strings
        that the front-end PivotTable can understand.
        """
        t = getattr(col, "type", None)

        # UUID (Postgres)
        if isinstance(t, PG_UUID):
            return "uuid"

        # Basic scalar types
        if isinstance(t, String):
            return "string"

        if isinstance(t, Integer):
            return "number"

        if isinstance(t, DateTime):
            return "datetime"

        # Enum is stored as string (because you used .value)
        # or native enum in PG, but for the front-end it's effectively a string.
        from sqlalchemy import Enum as SAEnum
        if isinstance(t, SAEnum):
            return "enum"

        # Fallback
        return "string"


    # --- RELATIONSHIP-AWARE VALIDATION (TO_OBJECT) ---
    def to_object(self):
        pydantic_cls = getattr(self, '__pydantic__', None)
        # Clean dict: exclude SA internal state and handle spatial binary
        data = {}
        for k, v in self.__dict__.items():
            if k.startswith('_'): continue
            if isinstance(v, WKBElement):
                data[k] = to_shape(v)  # Convert to Shapely for Pydantic
            else:
                data[k] = v

        if pydantic_cls:
            # RELATIONSHIP AWARENESS:
            # Check for fields defined in Pydantic but missing from __dict__ (lazy relationships)
            expected_fields = set(pydantic_cls.model_fields.keys())
            for field in expected_fields:
                if field not in data and hasattr(self, field):
                    rel_val = getattr(self, field)
                    # Recursively convert list of models or single model if they have to_object
                    if isinstance(rel_val, list):
                        data[field] = [i.to_object() if hasattr(i, 'to_object') else i for i in rel_val]
                    else:
                        data[field] = rel_val.to_object() if hasattr(rel_val, 'to_object') else rel_val

            return pydantic_cls.model_validate(data)
        return data

    # --- BULK CONVERSION (DATAFRAME) ---
    @classmethod
    def to_gdf(cls, qs: Union[Query, Select] = None, geom_col_name: str = None) -> gpd.GeoDataFrame:
        """
        Explicit method to return a GeoDataFrame.
        Automatically detects geom column and SRID.
        Raises Exception if multiple geometries exist and none are specified.
        """
        geom_cols = cls.get_geom_cols()

        if not geom_cols:
            raise ValueError(f"Model {cls.__name__} has no Geometry columns defined.")

        # Logic for multiple geometry columns
        if len(geom_cols) > 1 and geom_col_name is None:
            raise AmbiguousGeometryError(
                f"Model {cls.__name__} has multiple geometry columns: {list(geom_cols.keys())}. "
                "Please specify 'geom_col_name' explicitly."
            )

        # Use specified column or the only one available
        target_col_name = geom_col_name or list(geom_cols.keys())[0]

        if target_col_name not in geom_cols:
            raise KeyError(f"Column '{target_col_name}' is not a Geometry column in {cls.__name__}.")

        target_srid = geom_cols[target_col_name].type.srid

        return cls.to_dataframe(
            qs=qs,
            is_spatial=True,
            geom_col=target_col_name,
            srid=target_srid
        )

    from sqlalchemy import text, Select
    from sqlalchemy.orm import Query

    @classmethod
    def to_dataframe(
            cls,
            qs: Union[Query, Select, str] = None,  # Added str here
            is_spatial: bool = True,
            geom_col: str = None,
            srid: int = None,
            params: dict = None  # Added for safe parameter binding in raw SQL
    ) -> Union[pd.DataFrame, gpd.GeoDataFrame]:

        with cls.managed_session() as session:
            # 1. Logic to handle different query types
            if qs is None:
                objs = session.query(cls).all()
                data_dicts = [{k: v for k, v in obj.__dict__.items() if k != '_sa_instance_state'} for obj in objs]

            elif isinstance(qs, str):
                # Execute raw SQL string
                result = session.execute(text(qs), params or {})
                # Use mappings() to get dict-like rows immediately
                data_dicts = [dict(row) for row in result.mappings()]

            elif isinstance(qs, Select):
                # Execute SQLAlchemy 2.0 Select object
                result = session.execute(qs)
                # Check if we are selecting full ORM objects or specific columns
                if qs.column_descriptions and qs.column_descriptions[0]['entity'] == cls:
                    objs = result.scalars().all()
                    data_dicts = [{k: v for k, v in obj.__dict__.items() if k != '_sa_instance_state'} for obj in objs]
                else:
                    data_dicts = [dict(row) for row in result.mappings()]

            else:
                # Handle legacy ORM Query objects
                objs = qs.all() if hasattr(qs, 'all') else qs
                data_dicts = [{k: v for k, v in obj.__dict__.items() if k != '_sa_instance_state'} for obj in objs]

        # 2. Spatial Conversion Logic (Remains largely the same)
        if is_spatial and cls._Meta.is_spatial:
            if not geom_col or not srid:
                detected_name, detected_srid = cls.get_geom_col_name_srid()
                geom_col, srid = geom_col or detected_name, srid or detected_srid

            if geom_col and data_dicts:
                for row in data_dicts:
                    val = row.get(geom_col)
                    if val is not None:
                        # Convert WKB (binary) to Shapely for GeoPandas
                        if isinstance(val, WKBElement):
                            row[geom_col] = wkb.loads(bytes(val.data), hex=True)
                        elif isinstance(val, bytes):
                            row[geom_col] = wkb.loads(val)

                return gpd.GeoDataFrame(data_dicts, geometry=geom_col, crs=f"EPSG:{srid}")

        return pd.DataFrame(data_dicts)


    @classmethod
    def to_geojson(cls, qs: Union[Query, Select] = None) -> dict:
        """
        Converts the model data to a GeoJSON-compliant dictionary.
        """
        gdf = cls.to_gdf(qs=qs)
        if gdf.empty:
            return {"type": "FeatureCollection", "features": []}

        # The __geo_interface__ is a property of GeoDataFrames that
        # follows the GeoJSON standard.
        return gdf.__geo_interface__

    @classmethod
    def unary_union_to_gdf(cls, qs: Union[Query, Select] = None, session: Session = None) -> Optional[gpd.GeoDataFrame]:
        geom_cols = cls.get_geom_cols()
        if not geom_cols: return None
        g_name = list(geom_cols.keys())[0]
        srid = geom_cols[g_name].type.srid

        with cls.managed_session(session) as s:
            if qs is not None:
                result = s.query(func.ST_Union(qs.subquery().c[g_name])).scalar()
            else:
                result = s.query(func.ST_Union(getattr(cls, g_name))).scalar()

            if result is None: return None
            return gpd.GeoDataFrame(geometry=[cls.convert_wkb(result)], crs=f"EPSG:{srid}")

    # --- SERVER-SIDE SPATIAL UTILITIES ---

    @staticmethod
    def obj_to_shapely(obj, geom_col='geom'):
        """Converts a model instance's geometry to a Shapely object."""
        data = getattr(obj, geom_col)
        if isinstance(data, WKBElement):
            return wkb.loads(bytes(data.data), hex=True)
        return data

    # @staticmethod
    # def obj_to_shapely(obj, geom_col='geom'):
    #     return wkb.loads(obj[geom_col].desc)

    @staticmethod
    def obj_to_envelop(obj, session: Session, in_latlon: bool = False, geom_col='geom'):
        """
        Calculates the Bounding Box (Envelope) via PostGIS.
        Optionally transforms to Lat/Lon (EPSG:4326).
        """
        target_geom = getattr(obj, geom_col)
        if in_latlon:
            target_geom = func.ST_Transform(target_geom, 4326)

        # ST_Envelope returns a polygon representing the bounding box
        envelope = session.scalar(func.ST_Envelope(target_geom))

        if envelope is not None:
            return wkb.loads(bytes(envelope.data), hex=True)
        return None

    @staticmethod
    def obj_to_xy(obj, session: Session, in_latlon: bool = False, geom_col='geom'):
        """
        Extracts X and Y coordinates.
        If the geometry is a Polygon/MultiPolygon, it uses the Centroid.
        """
        target_geom = getattr(obj, geom_col)
        if in_latlon:
            target_geom = func.ST_Transform(target_geom, 4326)

        geom_type = session.scalar(func.GeometryType(target_geom))

        if geom_type in ['POLYGON', 'MULTIPOLYGON']:
            point_geom = func.ST_Centroid(target_geom)
        else:
            point_geom = target_geom

        x = session.scalar(func.ST_X(point_geom))
        y = session.scalar(func.ST_Y(point_geom))

        return x, y

    @classmethod
    def obj_to_gdf(cls, obj, geom_col_name=None) -> Union[gpd.GeoDataFrame, pd.DataFrame]:
        """
        Convert a single model instance to a GeoDataFrame.
        Handles date serialization and geometry conversion.
        """
        # We leverage the existing to_object logic to get clean data
        data = obj.to_object()

        # Determine geometry column and SRID
        g_cols = cls.get_geom_cols()
        if not g_cols:
            return pd.DataFrame([data])

        target_g = geom_col_name or list(g_cols.keys())[0]
        srid = g_cols[target_g].type.srid

        # Ensure dates are serialized for the DataFrame
        for k, v in data.items():
            if isinstance(v, (date, datetime)):
                data[k] = v.isoformat()

        return gpd.GeoDataFrame([data], geometry=target_g, crs=f"EPSG:{srid}")

    # --- STRING & WKB PARSING ---

    @classmethod
    def extent_box_2_tuple(cls, box_string: str) -> Tuple[float, float, float, float]:
        """Parses a PostGIS 'BOX(xmin ymin, xmax ymax)' string into a tuple."""
        # Standard PostGIS BOX format: BOX(1 2,3 4)
        values = box_string.replace('BOX(', '').replace(')', '').split(",")
        xmin, ymin = map(float, values[0].split())
        xmax, ymax = map(float, values[1].split())
        return (xmin, ymin, xmax, ymax)

    @staticmethod
    def convert_wkb_element_to_gdf(wkb_element: WKBElement) -> gpd.GeoDataFrame:
        """Converts a single WKBElement into a single-row GeoDataFrame."""
        geometry = wkb.loads(bytes(wkb_element.data), hex=True)
        srid = wkb_element.srid
        return gpd.GeoDataFrame([{'geometry': geometry}], crs=f'EPSG:{srid}')

    # --- SQL DEBUGGING ---

    @staticmethod
    def convert_to_sql_query(qs, engine) -> str:
        """Compiles a query object into a raw SQL string using the engine's dialect."""
        compiled = qs.compile(compile_kwargs={"literal_binds": True}, dialect=engine.dialect)
        return str(compiled)

    @classmethod
    def get_query_string(cls, qs):
        """Specifically compiles a query for the PostgreSQL dialect."""
        from sqlalchemy.dialects import postgresql
        return qs.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True}
        )

    # --- ADVANCED SPATIAL FILTERING ---
    @classmethod
    def get_result_set_under_aoi(cls, aoi_gdf: gpd.GeoDataFrame, geom_col: InstrumentedAttribute) -> gpd.GeoDataFrame:
        srid = geom_col.type.srid
        geom_col_name = geom_col.name
        if aoi_gdf.crs.to_epsg() != srid:
            aoi_gdf = aoi_gdf.to_crs(epsg=srid)

        aoi_geom_wkt = aoi_gdf.unary_union.wkt

        with cls.managed_session() as session:
            query = session.query(cls).filter(
                func.ST_Intersects(getattr(cls, geom_col_name), func.ST_GeomFromText(aoi_geom_wkt, srid))
            )
            return cls.to_dataframe(qs=query, is_spatial=True, geom_col=geom_col_name, srid=srid)




