"""
DBQuery Utility
---------------
A high-performance query execution suite for SQLAlchemy 2.0.
Bridges the gap between raw database results and high-level GIS/Data structures.

CORE CAPABILITIES:
1. Postgres Turbo Path: Offloads GeoJSON and MVT aggregation to the database (C-level speed).
2. Pydantic v2 Integration: Automatic ORM-to-Schema validation via __pydantic__ attributes.
3. Spatial Analysis: Native support for GeoPandas (GDF) and Mapbox Vector Tiles (MVT).
4. Safety: Enforced single-row fetching and result mapping to prevent data leaks.

---

### USAGE EXAMPLES:

#### 1. Standard REST API (Pydantic)
    stmt = select(User).where(User.is_active == True)
    # Infers Pydantic model from User.__pydantic__
    users = DBQuery.execute_query_all_as_pydantic(session, stmt)

    # Ensures business logic integrity
try:
    user_data = DBQuery.execute_query_as_one(
        session,
        "SELECT * FROM users WHERE email=:e",
        params={"e": "test@test.com"},
        raise_if_multiple=True
    )
except ValueError as e:
    # Triggers if a duplicate email exists in the DB
    print(f"Data Integrity Error: {e}")

#### 2. High-Performance Web Mapping (GeoJSON)
    # Aggregated entirely in Postgres; returns a single FeatureCollection
    geojson = DBQuery.execute_query_as_geojson(session, select(Parcel))

#### 3. Tiled Web Mapping (MVT)
    # Returns binary Protobuf data for Mapbox/Leaflet
    tile_data = DBQuery.execute_query_as_mvt(db, stmt, z, x, y, layer_name="parcels")

#### 4. Backend Spatial Analysis (GDF)
    # Returns a GeoDataFrame with Shapely geometries
    gdf = DBQuery.execute_query_as_gdf(session, select(CatchmentArea))

#### 5. Data Science & Exports (Pandas)
    # Returns a DataFrame with Enum values cleaned to strings
    df = DBQuery.execute_stmt_as_df(session, stmt, enum_columns=["status"])

---
@router.get("/tiles/{z}/{x}/{y}.pbf")
def get_vector_tiles(z: int, x: int, y: int, db: Session = Depends(get_db)):
    # Define the query for the data you want on the map
    stmt = select(Parcel).where(Parcel.active == True)

    tile_data = DBQuery.execute_query_as_mvt(
        db, stmt, z, x, y,
        layer_name="parcels",
        geom_col="geom"
    )

    return Response(content=tile_data, media_type="application/x-protobuf")

### OUTPUT FORMAT MAPPING:

| Format    | Method                         | Primary Use Case                                   |
| :-------- | :----------------------------- | :------------------------------------------------- |
| Pydantic  | `execute_query_as_pydantic`    | Standard JSON REST API responses                   |
| GeoJSON   | `execute_query_as_geojson`     | Web maps with small spatial datasets (<5MB)        |
| MVT       | `execute_query_as_mvt`         | Massive spatial datasets (Slippy map tiles)        |
| GDF       | `execute_query_as_gdf`         | Backend spatial analysis (Buffer, Join, Overlay)   |
| DataFrame | `execute_stmt_as_df`           | Statistics, Pivot tables, and CSV/Excel exports    |
"""

import json
import re
from math import ceil
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
from typing import Dict, Any, List, Optional, Union, Type, TypeVar

from shapely import wkb
from sqlalchemy import text, Select, select, Table, Engine, func
from sqlalchemy.exc import SQLAlchemyError, ResourceClosedError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.base import Executable

# T represents the Pydantic Model type
T = TypeVar("T")


class DBQuery:

    @staticmethod
    def _infer_entity_from_select(query: Select) -> Optional[Type[Any]]:
        # Works for standard ORM selects: select(MyModel) or select(MyModel).options(...)
        try:
            desc = query.column_descriptions
            if not desc:
                return None
            return desc[0].get("entity")
        except Exception:
            return None

    @staticmethod
    def execute_stmt_as_df(
            session: Session,
            stmt: Union[str, Select, Table],
            enum_columns: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """Executes a statement and returns a Pandas DataFrame."""
        try:
            if isinstance(stmt, Select):
                rs = session.execute(stmt)
            elif isinstance(stmt, Table):
                rs = session.execute(select(stmt))
            else:
                rs = session.execute(text(stmt))

            df = pd.DataFrame(rs.mappings().all())

            if not df.empty and enum_columns:
                for col in enum_columns:
                    if col in df.columns:
                        df[col] = df[col].apply(lambda x: x.value if hasattr(x, 'value') else x)
            return df
        except SQLAlchemyError as e:
            print(f"Error in execute_stmt_as_df: {e}")
            return pd.DataFrame()

    @staticmethod
    def execute_query_as_geojson(
            session: Session,
            stmt: Union[str, Select],
            geom_col: str = "geom",
            id_col: str = "id"
    ) -> Dict[str, Any]:
        """
        Returns a GeoJSON FeatureCollection.
        If using PostgreSQL, it uses a high-speed recursive JSON builder.
        """
        engine = session.get_bind()

        # --- HIGH SPEED POSTGRES PATH ---
        if engine.dialect.name == 'postgresql':
            # Wrap the existing query in a PostGIS GeoJSON builder
            raw_sql = str(stmt.compile(compile_kwargs={"literal_binds": True})) if not isinstance(stmt, str) else stmt

            pg_geojson_sql = f"""
                SELECT jsonb_build_object(
                    'type',     'FeatureCollection',
                    'features', COALESCE(jsonb_agg(features.feature), '[]'::jsonb)
                )
                FROM (
                  SELECT jsonb_build_object(
                    'type',       'Feature',
                    'id',         {id_col},
                    'geometry',   ST_AsGeoJSON({geom_col})::jsonb,
                    'properties', to_jsonb(inputs) - '{geom_col}'
                  ) AS feature
                  FROM ({raw_sql}) inputs
                ) features;
            """
            result = session.execute(text(pg_geojson_sql)).scalar()
            return result if result else {"type": "FeatureCollection", "features": []}

        # --- FALLBACK PATH (SQLite or other) ---
        result = session.execute(text(stmt) if isinstance(stmt, str) else stmt)
        features = []
        for row in result.mappings():
            geom = row.get(geom_col)
            if not geom: continue
            features.append({
                "type": "Feature",
                "geometry": json.loads(geom) if isinstance(geom, str) else geom,
                "properties": {k: v for k, v in row.items() if k != geom_col},
                "id": row.get(id_col)
            })
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    def _get_pydantic_model(orm_model: Type[Any]) -> Type[T]:
        pydantic_cls = getattr(orm_model, "__pydantic__", None)
        if not pydantic_cls:
            raise ValueError(f"Model {orm_model.__name__} has no __pydantic__ attribute.")
        return pydantic_cls

    @staticmethod
    def execute_query_as_pydantic(
            session: Session,
            query: Select,
            model: Optional[Type[Any]] = None,
            raise_if_none: bool = False
    ) -> Optional[T]:
        """Executes ORM query and validates against the model's __pydantic__ class."""
        orm_model = model or query.column_descriptions[0].get("entity")
        pydantic_cls = DBQuery._get_pydantic_model(orm_model)

        obj = session.execute(query).scalars().first()
        if obj is None:
            if raise_if_none: raise ValueError("No record found.")
            return None

        return pydantic_cls.model_validate(obj, from_attributes=True)

    @staticmethod
    def execute_query_all_as_pydantic(
            session: Session,
            query: Select,
            model: Optional[Type[Any]] = None
    ) -> List[T]:
        """Executes ORM query and returns a list of Pydantic instances."""
        orm_model = model or query.column_descriptions[0].get("entity")
        pydantic_cls = DBQuery._get_pydantic_model(orm_model)

        objs = session.execute(query).scalars().all()
        return [pydantic_cls.model_validate(o, from_attributes=True) for o in objs]




    # NEW: return exactly one row (as a dict) or None
    @staticmethod
    def execute_query_as_one(
            session: Session,
            query: Union[str, Select],
            params: Optional[dict] = None,
            *,
            raise_if_none: bool = False,
            raise_if_multiple: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a query and return one row as a dict (or None).

        Args:
            session: SQLAlchemy Session.
            query: raw SQL string or SQLAlchemy Select.
            params: bound parameters for the query.
            raise_if_none: if True, raise ValueError when no row is found.
            raise_if_multiple: if True, raise ValueError when more than one row is found.

        Returns:
            A dict representing the single row, or None if no row (unless raise_if_none=True).
        """
        # Execute
        if isinstance(query, str):
            result = session.execute(text(query), params or {})
        else:
            result = session.execute(query, params or {})

        maps = result.mappings()

        if raise_if_multiple:
            # Materialize all to check multiplicity
            rows = maps.all()
            if not rows:
                if raise_if_none:
                    raise ValueError("No rows returned for query.")
                return None
            if len(rows) > 1:
                raise ValueError(f"Expected at most one row, got {len(rows)}.")
            return dict(rows[0])

        # Not enforcing uniqueness; just get first/one_or_none
        row = maps.one_or_none() if raise_if_none else maps.first()
        if row is None:
            if raise_if_none:
                raise ValueError("No rows returned for query.")
            return None
        return dict(row)

    @staticmethod
    async def execute_query_as_geojson_async(
            session: AsyncSession,
            stmt: Union[str, Select],
            geom_col: str = "geom",
            id_col: str = "id"
    ) -> Dict[str, Any]:
        """
        Async version of the high-speed GeoJSON builder.
        Optimized for PostgreSQL.
        """
        engine = session.get_bind()

        if engine.dialect.name == 'postgresql':
            raw_sql = str(stmt.compile(compile_kwargs={"literal_binds": True})) if not isinstance(stmt, str) else stmt

            pg_geojson_sql = f"""
                    SELECT jsonb_build_object(
                        'type',     'FeatureCollection',
                        'features', COALESCE(jsonb_agg(features.feature), '[]'::jsonb)
                    )
                    FROM (
                      SELECT jsonb_build_object(
                        'type',       'Feature',
                        'id',         {id_col},
                        'geometry',   ST_AsGeoJSON({geom_col})::jsonb,
                        'properties', to_jsonb(inputs) - '{geom_col}'
                      ) AS feature
                      FROM ({raw_sql}) inputs
                    ) features;
                """
            result = await session.execute(text(pg_geojson_sql))
            return result.scalar() or {"type": "FeatureCollection", "features": []}

        # Fallback for Async SQLite
        result = await session.execute(text(stmt) if isinstance(stmt, str) else stmt)
        features = []
        for row in result.mappings():
            geom = row.get(geom_col)
            if not geom: continue
            features.append({
                "type": "Feature",
                "geometry": json.loads(geom) if isinstance(geom, str) else geom,
                "properties": {k: v for k, v in row.items() if k != geom_col},
                "id": row.get(id_col)
            })
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    def paginate_query(
            session: Session,
            query: Select,
            page: int = 1,
            page_size: int = 20
    ) -> Dict[str, Any]:
        """
        Wraps a query with Limit/Offset and calculates pagination metadata.
        Returns a dict compatible with frontend pagination components.
        """
        if page < 1: page = 1

        # 1. Get total count using a subquery for accuracy
        count_stmt = select(func.count()).select_from(query.subquery())
        total_items = session.execute(count_stmt).scalar() or 0

        # 2. Apply Limit and Offset
        total_pages = ceil(total_items / page_size) if total_items > 0 else 0
        offset = (page - 1) * page_size

        paginated_stmt = query.limit(page_size).offset(offset)

        # 3. Fetch data (Inferring Pydantic if possible)
        try:
            items = DBQuery.execute_query_all_as_pydantic(session, paginated_stmt)
        except ValueError:
            # Fallback to dicts if no Pydantic model is found
            items = [dict(r) for r in session.execute(paginated_stmt).mappings().all()]

        return {
            "items": items,
            "metadata": {
                "total_items": total_items,
                "total_pages": total_pages,
                "current_page": page,
                "page_size": page_size,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        }

    @staticmethod
    def execute_query_as_gdf(
            session: Session,
            stmt: Union[str, Select],
            params: dict = None,
            geom_col: str = "geom",
            crs: str = "EPSG:4326"
    ) -> gpd.GeoDataFrame:
        # 1. Execute
        executable_stmt = text(stmt) if isinstance(stmt, str) else stmt
        rs = session.execute(executable_stmt, params or {})

        # 2. Convert to DataFrame
        df = pd.DataFrame(rs.mappings().all())

        if df.empty:
            return gpd.GeoDataFrame(columns=df.columns, crs=crs)

        # 3. Robust Geometry Parsing
        if geom_col in df.columns:
            def parse_geom(x):
                if x is None: return None
                # Handle MemoryView (SQLAlchemy/PostgreSQL binary)
                if hasattr(x, 'data'):
                    return wkb.loads(bytes(x.data))
                # Handle Hex Strings (What you are seeing in your logs)
                if isinstance(x, str):
                    return wkb.loads(x, hex=True)
                # Handle raw Bytes
                if isinstance(x, bytes):
                    return wkb.loads(x)
                return x

            df[geom_col] = df[geom_col].apply(parse_geom)
            return gpd.GeoDataFrame(df, geometry=geom_col, crs=crs)

        raise ValueError(f"Geometry column '{geom_col}' not found in query results.")

    @staticmethod
    def execute_query_as_mvt(
            session: Session,
            stmt: Union[str, Select],
            z: int,
            x: int,
            y: int,
            geom_col: str = "geom",
            layer_name: str = "default_layer",
            buffer: int = 64,
            extent: int = 4096,
            resolution: float = 0  # New Parameter
    ) -> bytes:
        """
        High-speed MVT generation via PostGIS with geometry simplification.
        """
        engine = session.get_bind()
        if engine.dialect.name != 'postgresql':
            raise NotImplementedError("MVT generation is only supported on PostgreSQL/PostGIS.")

        raw_sql = str(stmt.compile(compile_kwargs={"literal_binds": True})) if not isinstance(stmt, str) else stmt

        # 1. Geometry logic: Apply simplification if resolution is provided
        # We use ST_SnapToGrid or ST_Simplify to reduce vertex count based on resolution
        if resolution > 0:
            geom_expression = f'ST_Simplify("{geom_col}", {resolution})'
        else:
            geom_expression = f'"{geom_col}"'

        # 2. Optimized MVT Query
        # Note: We use ST_Transform on the simplified geometry to move it to Web Mercator (3857)
        mvt_sql = f"""
            WITH 
            bounds AS (
              SELECT ST_TileEnvelope({z}, {x}, {y}) AS envelope
            ),
            mvt_geoms AS (
              SELECT 
                ST_AsMVTGeom(
                    ST_Transform({geom_expression}, 3857), 
                    bounds.envelope, 
                    {extent}, 
                    {buffer}, 
                    true
                ) AS mvt_geom,
                t.*
              FROM ({raw_sql}) AS t, bounds
              WHERE ST_Intersects(ST_Transform("{geom_col}", 3857), bounds.envelope)
            )
            SELECT ST_AsMVT(mvt_geoms.*, '{layer_name}', {extent}, 'mvt_geom') FROM mvt_geoms;
        """

        result = session.execute(text(mvt_sql)).scalar()
        return bytes(result) if result else b""

    @staticmethod
    async def execute_query_as_mvt_async(
            session: AsyncSession,
            stmt: Union[str, Select],
            z: int,
            x: int,
            y: int,
            geom_col: str = "geom",
            layer_name: str = "default_layer",
            buffer: int = 64,
            extent: int = 4096
    ) -> bytes:
        """Async version of high-speed MVT generation via PostGIS."""
        engine = session.get_bind()
        if engine.dialect.name != 'postgresql':
            raise NotImplementedError("MVT generation is only supported on PostgreSQL/PostGIS.")

        raw_sql = str(stmt.compile(compile_kwargs={"literal_binds": True})) if not isinstance(stmt, str) else stmt

        mvt_sql = f"""
                WITH 
                bounds AS (SELECT ST_TileEnvelope({z}, {x}, {y}) AS envelope),
                mvt_geoms AS (
                  SELECT 
                    ST_AsMVTGeom(ST_Transform(t.{geom_col}, 3857), bounds.envelope, {extent}, {buffer}, true) AS mvt_geom,
                    t.*
                  FROM ({raw_sql}) AS t, bounds
                  WHERE ST_Intersects(ST_Transform(t.{geom_col}, 3857), bounds.envelope)
                )
                SELECT ST_AsMVT(mvt_geoms.*, '{layer_name}', {extent}, 'mvt_geom') FROM mvt_geoms;
            """

        result = await session.execute(text(mvt_sql))
        return bytes(result.scalar()) if result else b""

    @classmethod
    def safe_ident(cls, name: str) -> str:
        """
        Very small safety net for identifiers (table/column).
        Allows schema.table, letters, digits, underscore.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", name):
            raise ValueError(f"Unsafe SQL identifier: {name!r}")
        return name


    @staticmethod
    def execute_query_as_dicts(
            session: Session,
            query: Union[str, Executable, Table],
            params: Optional[dict] = None
    ) -> List[Dict[str, Any]]:
        """
        Supports:
        - raw SQL string
        - Select / union / subqueries (Executable)
        - Table
        """
        if isinstance(query, str):
            result = session.execute(text(query), params or {})
        elif isinstance(query, Table):
            result = session.execute(select(query), params or {})
        else:
            result = session.execute(query, params or {})

        try:
            return [dict(row) for row in result.mappings()]
        except ResourceClosedError:
            return []

    @staticmethod
    def execute_query_as_objects(
            session: Session,
            query: Union[str, Executable, Table],
            params: Optional[dict] = None
    ) -> List[SimpleNamespace]:
        return [
            SimpleNamespace(**row)
            for row in DBQuery.execute_query_as_dicts(session, query, params)
        ]

    @staticmethod
    def execute_query_as_objects(
            session: Session,
            query: Union[str, Executable, Table],
            params: Optional[dict] = None
    ) -> List[SimpleNamespace]:
        """
                 Executable could Select, select, compound union etc
                """
        return [
            SimpleNamespace(**row)
            for row in DBQuery.execute_query_as_dicts(session, query, params)
        ]