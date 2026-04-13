from __future__ import annotations

from contextlib import contextmanager

from types import SimpleNamespace
from typing import Any, Dict, Generator, List, Literal, Optional, Tuple, Union

import geopandas as gpd
import pandas as pd
from pydantic import BaseModel
from shapely import wkb
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import ColumnElement, DateTime, Engine, Integer, Select, String, func, inspect as sa_inspect, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Query, Session

from geoalchemy2 import Geometry, WKBElement
from geoalchemy2.shape import to_shape

from app.damapql.settings import DCH_DATABASES, DCH_DB_KEY
from app.damapql.utils.adapters.deps import (
    _sync_session_factories,
    get_sync_engine_instance,
    sync_session_scope,
)
from settings import DA_APPS


class AmbiguousGeometryError(Exception):
    pass


class ModelHelpersMixin:
    class _Meta:
        is_spatial = True
        db_key = None
        app_label = None

    # -------------------------------------------------------
    # REPRESENTATION HELPERS
    # -------------------------------------------------------
    def as_representation(
        self,
        mode: Literal["orm", "object", "dict"] = "orm",
    ):
        if mode == "orm":
            return self
        if mode == "object":
            return self.to_object()
        if mode == "dict":
            return self.to_dict()
        raise ValueError(f"Invalid mode: {mode}")

    @staticmethod
    def normalize_geometry_value(
        value: Any,
        output: Literal["shapely", "wkt", "geojson"] = "shapely",
    ) -> Any:
        """
        Normalize geometry across common representations.

        Accepts:
        - Shapely geometry
        - GeoAlchemy WKBElement
        - WKB bytes / bytearray / memoryview
        - GeoJSON dict
        - WKT string
        """
        if value is None:
            return None

        geom = None

        if isinstance(value, BaseGeometry):
            geom = value

        elif isinstance(value, WKBElement):
            geom = to_shape(value)

        elif isinstance(value, (bytes, bytearray)):
            try:
                geom = wkb.loads(bytes(value))
            except Exception:
                geom = None

        elif isinstance(value, memoryview):
            try:
                geom = wkb.loads(bytes(value))
            except Exception:
                geom = None

        elif isinstance(value, dict):
            try:
                geom = shape(value)
            except Exception:
                geom = None

        elif isinstance(value, str):
            try:
                from shapely import wkt as shapely_wkt
                geom = shapely_wkt.loads(value)
            except Exception:
                geom = None

        if geom is None:
            return None

        if output == "shapely":
            return geom
        if output == "wkt":
            return geom.wkt
        if output == "geojson":
            return mapping(geom)

        raise ValueError(f"Unsupported geometry output: {output}")

    # -------------------------------------------------------
    # INTERNAL SERVICE LOCATOR
    # -------------------------------------------------------
    @classmethod
    def get_engine(cls, app_label: str = None, db_key: str = None) -> Engine:
        if db_key is None:
            db_key = cls.get_db_key(app_label)
        return get_sync_engine_instance(db_key)

    @staticmethod
    def engine_to_db_config(engine: Engine) -> dict:
        url = engine.url
        return {
            "engine": url.drivername,
            "host": url.host,
            "port": url.port,
            "name": url.database,
            "user": url.username,
            "password": url.password,
        }

    @classmethod
    def get_session(cls) -> Session:
        db_key = cls.get_db_key()
        get_sync_engine_instance(db_key)
        SessionLocal = _sync_session_factories[db_key]
        return SessionLocal()

    @classmethod
    @contextmanager
    def managed_session(
        cls,
        session: Session = None,
        app_label: str = None,
    ) -> Generator[Session, None, None]:
        db_key = cls.get_db_key(app_label)
        if session:
            yield session
        else:
            with sync_session_scope(db_key) as s:
                yield s

    @classmethod
    def get_db_key(cls, app_label: str = None) -> str:
        if hasattr(cls._Meta, "db_key") and cls._Meta.db_key is not None:
            return cls._Meta.db_key

        if app_label is None:
            if hasattr(cls._Meta, "app_label") and cls._Meta.app_label is not None:
                app_label = getattr(cls._Meta, "app_label")
            else:
                app_label = cls.__module__.split(".")[-2]
                if app_label == "models":
                    app_label = cls.__module__.split(".")[-3]

        return DA_APPS.get(app_label, {}).get("db_key") or None

    @classmethod
    def get_db_params(cls, app_label: str = None) -> Dict[str, Any]:
        key = cls.get_db_key(app_label)
        return DCH_DATABASES.get(key, DCH_DATABASES.get(DCH_DB_KEY))

    # -------------------------------------------------------
    # MODEL METADATA HELPERS
    # -------------------------------------------------------
    @classmethod
    def get_conflict_fields(cls, unique_fields=None) -> List[str]:
        if unique_fields is None:
            unique_fields = getattr(cls._Meta, "unique_fields", None)

        if not unique_fields:
            unique_fields = cls.get_primary_key_fields()

        table_cols = cls.__table__.columns.keys()
        return [f for f in unique_fields if f in table_cols]

    @classmethod
    def get_primary_key_fields(cls) -> list[str]:
        return [col.name for col in sa_inspect(cls).primary_key]

    @classmethod
    def get_fields(cls, skip_fields=None):
        if skip_fields is None:
            skip_fields = []

        mapper = sa_inspect(cls)
        return [column.key for column in mapper.attrs if column.key not in skip_fields]

    @classmethod
    def get_geom_cols(cls) -> dict:
        if cls._Meta.is_spatial:
            return {
                name: col
                for name, col in cls.__table__.columns.items()
                if isinstance(col.type, Geometry)
            }
        return {}

    @classmethod
    def get_geom_col_name_srid(cls) -> Tuple[Optional[str], Optional[int]]:
        cols = cls.get_geom_cols()
        if cols:
            name = list(cols.keys())[0]
            return name, cols[name].type.srid
        return None, None

    @staticmethod
    def sa_type_to_js_type(col: ColumnElement[Any]) -> str:
        t = getattr(col, "type", None)

        if isinstance(t, PG_UUID):
            return "uuid"
        if isinstance(t, String):
            return "string"
        if isinstance(t, Integer):
            return "number"
        if isinstance(t, DateTime):
            return "datetime"

        from sqlalchemy import Enum as SAEnum
        if isinstance(t, SAEnum):
            return "enum"

        return "string"

    # -------------------------------------------------------
    # OBJECT / DICT CONVERSION
    # -------------------------------------------------------
    def to_object(self):
        pydantic_cls = getattr(self, "__pydantic__", None)
        data = {}

        for k, v in self.__dict__.items():
            if k.startswith("_"):
                continue

            if isinstance(v, WKBElement):
                data[k] = to_shape(v)
            else:
                data[k] = v

        if pydantic_cls:
            expected_fields = set(pydantic_cls.model_fields.keys())

            for field in expected_fields:
                if field not in data and hasattr(self, field):
                    rel_val = getattr(self, field)

                    if isinstance(rel_val, list):
                        data[field] = [
                            i.to_object() if hasattr(i, "to_object") else i
                            for i in rel_val
                        ]
                    else:
                        data[field] = (
                            rel_val.to_object()
                            if hasattr(rel_val, "to_object")
                            else rel_val
                        )

            return pydantic_cls.model_validate(data)

        return SimpleNamespace(**data)

    def to_dict(self):
        def _convert(obj):
            if hasattr(obj, "model_dump"):
                return obj.model_dump()

            if isinstance(obj, (list, tuple)):
                return [_convert(i) for i in obj]

            if hasattr(obj, "__dict__"):
                return {
                    k: _convert(v)
                    for k, v in vars(obj).items()
                    if not k.startswith("_")
                }

            if hasattr(obj, "__geo_interface__"):
                return obj.__geo_interface__

            return obj

        return _convert(self.to_object())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "cls":
        if not isinstance(data, dict):
            raise TypeError(f"{cls.__name__}.from_dict expects dict, got {type(data)}")

        table_columns = {c.name for c in cls.__table__.columns}
        geom_cols = cls.get_geom_cols()

        filtered = {}
        for k, v in data.items():
            if k not in table_columns:
                continue

            col = cls.__table__.columns[k]
            if isinstance(col.type, Geometry):
                filtered[k] = cls.normalize_geometry_value(v, output="shapely")
            else:
                filtered[k] = v

        return cls(**filtered)

    @classmethod
    def from_object(
        cls,
        obj: dict | SimpleNamespace | BaseModel,
        *,
        exclude_unset: bool = True,
    ) -> "cls":
        if isinstance(obj, BaseModel):
            data = obj.model_dump(exclude_unset=exclude_unset)
        elif isinstance(obj, dict):
            data = obj
        elif isinstance(obj, SimpleNamespace):
            data = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        else:
            raise TypeError(
                f"{cls.__name__}.from_object expects "
                f"dict | SimpleNamespace | BaseModel, got {type(obj)}"
            )

        return cls.from_dict(data)

    # -------------------------------------------------------
    # BULK SAVE
    # -------------------------------------------------------
    @classmethod
    def bulk_save(
        cls,
        data: list[dict | Any] | pd.DataFrame | gpd.GeoDataFrame,
        unique_fields: list[str] | None = None,
        app_label: str | None = None,
    ) -> int:
        if data is None:
            return 0

        if isinstance(data, (pd.DataFrame, gpd.GeoDataFrame)) and data.empty:
            return 0

        if isinstance(data, list) and not data:
            return 0

        table = cls.__table__
        table_columns = {c.name for c in table.columns}
        pk_fields = set(cls.get_primary_key_fields())
        conflict_fields = cls.get_conflict_fields(unique_fields)

        if not conflict_fields:
            raise ValueError(
                f"No conflict fields could be resolved for {cls.__name__}. "
                f"Define _Meta.unique_fields or ensure a primary key exists."
            )

        processed_data: list[dict] = []

        if isinstance(data, gpd.GeoDataFrame):
            processed_data = data.copy().to_dict("records")

        elif isinstance(data, pd.DataFrame):
            processed_data = data.to_dict("records")

        else:
            for item in data:
                if isinstance(item, dict):
                    row = item.copy()
                elif hasattr(item, "model_dump"):
                    row = item.model_dump()
                elif hasattr(item, "to_dict") and callable(item.to_dict):
                    row = item.to_dict()
                else:
                    row = {
                        col.name: getattr(item, col.name, None)
                        for col in table.columns
                    }

                processed_data.append(row)

        if not processed_data:
            return 0

        processed_data = [
            {k: v for k, v in row.items() if k in table_columns}
            for row in processed_data
        ]

        # Normalize geometry to WKT first. This makes insert/update consistent.
        normalized_rows: list[dict] = []
        for row in processed_data:
            new_row: dict = {}

            for c in table.columns:
                if c.name not in row:
                    continue

                val = row[c.name]
                if isinstance(c.type, Geometry):
                    new_row[c.name] = cls.normalize_geometry_value(val, output="wkt")
                else:
                    new_row[c.name] = val

            normalized_rows.append(new_row)

        engine = cls.get_engine(app_label=app_label)
        dialect = engine.dialect.name

        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(table).values(normalized_rows)

            # Convert WKT strings to geometry in both insert and update paths
            insert_values = {}
            for c in table.columns:
                if isinstance(c.type, Geometry):
                    srid = getattr(c.type, "srid", None)
                    insert_values[c.name] = (
                        func.ST_GeomFromText(stmt.excluded[c.name], srid)
                        if srid else func.ST_GeomFromText(stmt.excluded[c.name])
                    )

            update_dict = {}
            for c in table.columns:
                if c.name in pk_fields or c.name in conflict_fields:
                    continue

                if isinstance(c.type, Geometry):
                    srid = getattr(c.type, "srid", None)
                    update_dict[c.name] = (
                        func.ST_GeomFromText(stmt.excluded[c.name], srid)
                        if srid else func.ST_GeomFromText(stmt.excluded[c.name])
                    )
                else:
                    update_dict[c.name] = stmt.excluded[c.name]

            # Rebuild insert statement with SQL expressions for geometry columns
            values_with_geom = []
            for row in normalized_rows:
                shaped_row = {}
                for c in table.columns:
                    if c.name not in row:
                        continue

                    val = row[c.name]
                    if isinstance(c.type, Geometry):
                        if val is None:
                            shaped_row[c.name] = None
                        else:
                            srid = getattr(c.type, "srid", None)
                            shaped_row[c.name] = (
                                func.ST_GeomFromText(val, srid)
                                if srid else func.ST_GeomFromText(val)
                            )
                    else:
                        shaped_row[c.name] = val
                values_with_geom.append(shaped_row)

            stmt = pg_insert(table).values(values_with_geom)

            if update_dict:
                stmt = stmt.on_conflict_do_update(
                    index_elements=conflict_fields,
                    set_=update_dict,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=conflict_fields,
                )

        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            # SQLite path remains plain because PostGIS funcs are unavailable
            stmt = sqlite_insert(table).values(normalized_rows)

            update_dict = {
                c.name: stmt.excluded[c.name]
                for c in table.columns
                if c.name not in pk_fields and c.name not in conflict_fields
            }

            if update_dict:
                stmt = stmt.on_conflict_do_update(
                    index_elements=conflict_fields,
                    set_=update_dict,
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=conflict_fields,
                )

        else:
            from sqlalchemy import insert as base_insert
            stmt = base_insert(table).values(normalized_rows)

        with engine.begin() as conn:
            result = conn.execute(stmt)

        return result.rowcount if result.rowcount is not None else 0

    # -------------------------------------------------------
    # SAVE / DELETE
    # -------------------------------------------------------
    def save(
        self,
        session: Union[Session, AsyncSession] = None,
        app_label: str = None,
        unique_fields: List[str] = None,
        return_mode: Literal["orm", "object", "dict"] = "orm",
    ) -> Any:
        conflict_fields = self.get_conflict_fields(unique_fields)
        pk_fields = self.get_primary_key_fields()

        with self.managed_session(session, app_label) as s:
            existing = None

            if conflict_fields:
                criteria = {
                    f: getattr(self, f)
                    for f in conflict_fields
                    if getattr(self, f, None) is not None
                }

                if criteria:
                    existing = s.query(self.__class__).filter_by(**criteria).first()

            if not existing and pk_fields:
                pk_vals = [getattr(self, f, None) for f in pk_fields]

                if len(pk_fields) == 1 and pk_vals[0] is not None:
                    existing = s.get(self.__class__, pk_vals[0])

                elif len(pk_fields) > 1 and all(v is not None for v in pk_vals):
                    existing = s.get(self.__class__, tuple(pk_vals))

            if existing:
                current_data = self.to_dict()
                for col in self.__table__.columns.keys():
                    if col in pk_fields or col not in current_data:
                        continue
                    setattr(existing, col, getattr(self, col, None))
                target = existing
                is_add = False
            else:
                target = self
                is_add = True

            if isinstance(s, AsyncSession):
                raise TypeError(
                    f"{self.__class__.__name__}.save() received AsyncSession in a sync method. "
                    "Use save_async() or an async-managed path."
                )

            obj = self._save_sync(target, s, is_add)
            return obj.as_representation(return_mode)

    @staticmethod
    def _save_sync(obj, session: Session, is_add: bool):
        if is_add:
            session.add(obj)
        session.commit()
        session.refresh(obj)
        return obj

    @staticmethod
    async def _save_async(obj, session: AsyncSession, is_add: bool):
        if is_add:
            session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj

    def delete(self, session: Union[Session, AsyncSession] = None, app_label=None):
        with self.managed_session(session, app_label) as s:
            if isinstance(s, AsyncSession):
                return self._delete_async(self, s)
            s.delete(self)
            s.commit()
            return None

    @staticmethod
    async def _delete_async(obj, session):
        await session.delete(obj)
        await session.commit()

    # -------------------------------------------------------
    # DATAFRAME / GDF / GEOJSON
    # -------------------------------------------------------
    @classmethod
    def to_gdf(
        cls,
        qs: Union[Query, Select, List[Any], None] = None,
        geom_col_name: str = None,
        target_crs: int | None = None,
    ) -> Union[gpd.GeoDataFrame, pd.DataFrame]:
        with cls.managed_session() as session:
            if qs is None:
                objs = session.query(cls).all()
            elif isinstance(qs, Select):
                objs = session.execute(qs).scalars().all()
            elif isinstance(qs, Query):
                objs = qs.all()
            elif isinstance(qs, list):
                objs = qs
            else:
                raise TypeError(f"Unsupported query type: {type(qs)}")

        if not objs:
            return gpd.GeoDataFrame()

        records = []
        for obj in objs:
            if isinstance(obj, dict):
                records.append(obj)
            elif hasattr(obj, "to_dict"):
                records.append(obj.to_dict())
            else:
                records.append({
                    col.name: getattr(obj, col.name, None)
                    for col in cls.__table__.columns
                })

        if not records:
            return gpd.GeoDataFrame()

        g_cols = cls.get_geom_cols()
        if not g_cols:
            return pd.DataFrame(records)

        geom_col = geom_col_name or list(g_cols.keys())[0]
        source_srid = g_cols[geom_col].type.srid

        if source_srid is None:
            raise ValueError(f"{cls.__name__}: Geometry SRID is undefined")

        for row in records:
            if geom_col in row:
                row[geom_col] = cls.normalize_geometry_value(row[geom_col], output="shapely")

        gdf = gpd.GeoDataFrame(
            records,
            geometry=geom_col,
            crs=f"EPSG:{source_srid}",
        )

        if target_crs:
            if gdf.crs is None:
                raise ValueError("Cannot transform CRS: source CRS is undefined")
            gdf = gdf.to_crs(epsg=target_crs)

        return gdf

    @classmethod
    def to_dataframe(
        cls,
        qs: Union[Query, Select, str] = None,
        is_spatial: bool = True,
        geom_col: str = None,
        srid: int = None,
        params: dict = None,
    ) -> Union[pd.DataFrame, gpd.GeoDataFrame]:
        with cls.managed_session() as session:
            if qs is None:
                objs = session.query(cls).all()
                data_dicts = [obj.to_dict() if hasattr(obj, "to_dict") else {
                    k: v for k, v in obj.__dict__.items() if not k.startswith("_")
                } for obj in objs]

            elif isinstance(qs, str):
                result = session.execute(text(qs), params or {})
                data_dicts = [dict(row) for row in result.mappings()]

            elif isinstance(qs, Select):
                result = session.execute(qs)
                if qs.column_descriptions and qs.column_descriptions[0]["entity"] == cls:
                    objs = result.scalars().all()
                    data_dicts = [obj.to_dict() if hasattr(obj, "to_dict") else {
                        k: v for k, v in obj.__dict__.items() if not k.startswith("_")
                    } for obj in objs]
                else:
                    data_dicts = [dict(row) for row in result.mappings()]

            else:
                objs = qs.all() if hasattr(qs, "all") else qs
                data_dicts = [obj.to_dict() if hasattr(obj, "to_dict") else {
                    k: v for k, v in obj.__dict__.items() if not k.startswith("_")
                } for obj in objs]

        if is_spatial and cls._Meta.is_spatial:
            if not geom_col or not srid:
                detected_name, detected_srid = cls.get_geom_col_name_srid()
                geom_col, srid = geom_col or detected_name, srid or detected_srid

            if geom_col and data_dicts:
                for row in data_dicts:
                    val = row.get(geom_col)
                    if val is not None:
                        row[geom_col] = cls.normalize_geometry_value(val, output="shapely")

                return gpd.GeoDataFrame(data_dicts, geometry=geom_col, crs=f"EPSG:{srid}")

        return pd.DataFrame(data_dicts)

    @classmethod
    def to_geojson(cls, qs: Union[Query, Select] = None) -> dict:
        gdf = cls.to_gdf(qs=qs)
        if gdf.empty:
            return {"type": "FeatureCollection", "features": []}
        return gdf.__geo_interface__

    @classmethod
    def unary_union_to_gdf(
        cls,
        qs: Union[Query, Select] = None,
        session: Session = None,
    ) -> Optional[gpd.GeoDataFrame]:
        geom_cols = cls.get_geom_cols()
        if not geom_cols:
            return None

        g_name = list(geom_cols.keys())[0]
        srid = geom_cols[g_name].type.srid

        with cls.managed_session(session) as s:
            if qs is not None:
                result = s.query(func.ST_Union(qs.subquery().c[g_name])).scalar()
            else:
                result = s.query(func.ST_Union(getattr(cls, g_name))).scalar()

            if result is None:
                return None

            return gpd.GeoDataFrame(
                geometry=[cls.normalize_geometry_value(result, output="shapely")],
                crs=f"EPSG:{srid}",
            )

    # -------------------------------------------------------
    # SERVER-SIDE SPATIAL UTILITIES
    # -------------------------------------------------------
    @staticmethod
    def obj_to_shapely(obj, geom_col="geom"):
        return ModelHelpersMixin.normalize_geometry_value(
            getattr(obj, geom_col),
            output="shapely",
        )

    @staticmethod
    def obj_to_envelop(obj, session: Session, in_latlon: bool = False, geom_col="geom"):
        target_geom = getattr(obj, geom_col)
        if in_latlon:
            target_geom = func.ST_Transform(target_geom, 4326)

        envelope = session.scalar(func.ST_Envelope(target_geom))
        if envelope is not None:
            return ModelHelpersMixin.normalize_geometry_value(envelope, output="shapely")
        return None

    @staticmethod
    def obj_to_xy(obj, session: Session, in_latlon: bool = False, geom_col="geom"):
        target_geom = getattr(obj, geom_col)
        if in_latlon:
            target_geom = func.ST_Transform(target_geom, 4326)

        geom_type = session.scalar(func.GeometryType(target_geom))
        point_geom = func.ST_Centroid(target_geom) if geom_type in ["POLYGON", "MULTIPOLYGON"] else target_geom

        x = session.scalar(func.ST_X(point_geom))
        y = session.scalar(func.ST_Y(point_geom))
        return x, y

    # -------------------------------------------------------
    # STRING & WKB PARSING
    # -------------------------------------------------------
    @classmethod
    def extent_box_2_tuple(cls, box_string: str) -> Tuple[float, float, float, float]:
        values = box_string.replace("BOX(", "").replace(")", "").split(",")
        xmin, ymin = map(float, values[0].split())
        xmax, ymax = map(float, values[1].split())
        return (xmin, ymin, xmax, ymax)

    @staticmethod
    def convert_wkb_element_to_gdf(wkb_element: WKBElement) -> gpd.GeoDataFrame:
        geometry = ModelHelpersMixin.normalize_geometry_value(wkb_element, output="shapely")
        srid = wkb_element.srid
        return gpd.GeoDataFrame([{"geometry": geometry}], crs=f"EPSG:{srid}")

    # -------------------------------------------------------
    # SQL DEBUGGING
    # -------------------------------------------------------
    @staticmethod
    def convert_to_sql_query(qs, engine) -> str:
        compiled = qs.compile(compile_kwargs={"literal_binds": True}, dialect=engine.dialect)
        return str(compiled)

    @classmethod
    def get_query_string(cls, qs):
        from sqlalchemy.dialects import postgresql
        return qs.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )

    # -------------------------------------------------------
    # ADVANCED SPATIAL FILTERING
    # -------------------------------------------------------
    @classmethod
    def get_result_set_under_aoi(
        cls,
        aoi_gdf: gpd.GeoDataFrame,
        geom_col: InstrumentedAttribute,
    ) -> gpd.GeoDataFrame:
        srid = geom_col.type.srid
        geom_col_name = geom_col.name

        if aoi_gdf.crs.to_epsg() != srid:
            aoi_gdf = aoi_gdf.to_crs(epsg=srid)

        aoi_geom_wkt = aoi_gdf.unary_union.wkt

        with cls.managed_session() as session:
            query = session.query(cls).filter(
                func.ST_Intersects(
                    getattr(cls, geom_col_name),
                    func.ST_GeomFromText(aoi_geom_wkt, srid),
                )
            )
            return cls.to_dataframe(qs=query, is_spatial=True, geom_col=geom_col_name, srid=srid)