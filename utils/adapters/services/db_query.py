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

from __future__ import annotations

import json
import re
from math import ceil
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import geopandas as gpd
import pandas as pd
from shapely import wkb
from sqlalchemy import Engine, Table, func, select, text
from sqlalchemy.exc import ResourceClosedError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.selectable import Select

from digitalarzengine.utils.singletons import da_logger

# Type variable for Pydantic Models
T = TypeVar("T")


class DBQuery:

    @staticmethod
    def _infer_entity_from_select(query: Select) -> Optional[Type[Any]]:
        """Infers the root ORM Entity from a SQLAlchemy Select statement."""
        try:
            desc = query.column_descriptions
            if not desc:
                return None
            return desc[0].get("entity")
        except Exception:
            return None

    @staticmethod
    def _get_pydantic_model(orm_model: Type[Any]) -> Type[T]:
        """Extracts attached __pydantic__ schema from the ORM model."""
        pydantic_cls = getattr(orm_model, "__pydantic__", None)
        if not pydantic_cls:
            raise ValueError(f"Model {orm_model.__name__} has no __pydantic__ attribute.")
        return pydantic_cls

    # =========================================================================
    # 1. PANDAS & DATAFRAME UTILITIES
    # =========================================================================

    @staticmethod
    def execute_stmt_as_df(
        session: Session,
        stmt: Union[str, Executable, Table],
        enum_columns: Optional[List[str]] = None,
        params: Optional[dict] = None
    ) -> pd.DataFrame:
        """Executes a query and converts the mapped results into a Pandas DataFrame."""
        try:
            if isinstance(stmt, Table):
                rs = session.execute(select(stmt), params or {})
            elif isinstance(stmt, str):
                rs = session.execute(text(stmt), params or {})
            else:
                rs = session.execute(stmt, params or {})

            df = pd.DataFrame(rs.mappings().all())

            if not df.empty and enum_columns:
                for col in enum_columns:
                    if col in df.columns:
                        df[col] = df[col].apply(
                            lambda x: [v.value if hasattr(v, 'value') else v for v in x]
                            if isinstance(x, list)
                            else (x.value if hasattr(x, 'value') else x)
                        )
            return df
        except SQLAlchemyError as e:
            da_logger.error(f"Error in execute_stmt_as_df: {e}")
            return pd.DataFrame()

    # =========================================================================
    # 2. DICT, OBJECT, & ROW MAPPING UTILITIES
    # =========================================================================

    @staticmethod
    def execute_query_as_dicts(
        session: Session,
        query: Union[str, Executable, Table],
        params: Optional[dict] = None
    ) -> List[Dict[str, Any]]:
        """Executes a query and returns results as standard Python dictionaries."""
        if isinstance(query, str):
            result = session.execute(text(query), params or {})
        elif isinstance(query, Table):
            result = session.execute(select(query), params or {})
        else:
            result = session.execute(query, params or {})

        try:
            return [dict(row) for row in result.mappings().all()]
        except ResourceClosedError:
            return []

    @staticmethod
    def execute_query_as_dict(
        session: Session,
        query: Union[str, Executable, Table],
        params: Optional[dict] = None
    ) -> List[Dict[str, Any]]:
        """Alias for execute_query_as_dicts."""
        return DBQuery.execute_query_as_dicts(session, query, params)

    @staticmethod
    def execute_query_as_objects(
        session: Session,
        query: Union[str, Executable, Table],
        params: Optional[dict] = None
    ) -> List[SimpleNamespace]:
        """Executes query and wraps rows in SimpleNamespace for dot-attribute access."""
        return [
            SimpleNamespace(**row)
            for row in DBQuery.execute_query_as_dicts(session, query, params)
        ]

    @staticmethod
    def execute_query_as_one(
        session: Session,
        query: Union[str, Executable],
        params: Optional[dict] = None,
        *,
        raise_if_none: bool = False,
        raise_if_multiple: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Executes a query and returns a single row dictionary (or None)."""
        executable = text(query) if isinstance(query, str) else query
        result = session.execute(executable, params or {})
        maps = result.mappings()

        if raise_if_multiple:
            rows = maps.all()
            if not rows:
                if raise_if_none:
                    raise ValueError("No rows returned for query.")
                return None
            if len(rows) > 1:
                raise ValueError(f"Expected at most one row, got {len(rows)}.")
            return dict(rows[0])

        row = maps.one_or_none() if raise_if_none else maps.first()
        if row is None:
            if raise_if_none:
                raise ValueError("No rows returned for query.")
            return None
        return dict(row)

    # =========================================================================
    # 3. PYDANTIC INTEGRATION & PAGINATION
    # =========================================================================

    @staticmethod
    def execute_query_as_pydantic(
        session: Session,
        query: Select,
        model: Optional[Type[Any]] = None,
        raise_if_none: bool = False
    ) -> Optional[T]:
        """Executes ORM query and validates the single result using Pydantic v2."""
        orm_model = model or DBQuery._infer_entity_from_select(query)
        pydantic_cls = DBQuery._get_pydantic_model(orm_model)

        obj = session.execute(query).scalars().first()
        if obj is None:
            if raise_if_none:
                raise ValueError("No record found.")
            return None

        return pydantic_cls.model_validate(obj, from_attributes=True)

    @staticmethod
    def execute_query_all_as_pydantic(
        session: Session,
        query: Select,
        model: Optional[Type[Any]] = None
    ) -> List[T]:
        """Executes ORM query and validates all results using Pydantic v2."""
        orm_model = model or DBQuery._infer_entity_from_select(query)
        pydantic_cls = DBQuery._get_pydantic_model(orm_model)

        objs = session.execute(query).scalars().all()
        return [pydantic_cls.model_validate(o, from_attributes=True) for o in objs]

    @staticmethod
    def paginate_query(
        session: Session,
        query: Select,
        page: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        """Paginates a query and returns items alongside pagination metadata."""
        if page < 1:
            page = 1

        count_stmt = select(func.count()).select_from(query.subquery())
        total_items = session.execute(count_stmt).scalar() or 0

        total_pages = ceil(total_items / page_size) if total_items > 0 else 0
        offset = (page - 1) * page_size

        paginated_stmt = query.limit(page_size).offset(offset)

        try:
            items = DBQuery.execute_query_all_as_pydantic(session, paginated_stmt)
        except ValueError:
            items = DBQuery.execute_query_as_dicts(session, paginated_stmt)

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

    # =========================================================================
    # 4. GIS & SPATIAL DATA UTILITIES (GeoJSON, MVT, GDF)
    # =========================================================================

    @staticmethod
    def safe_ident(identifier: str) -> str:
        """Safely double-quotes PostgreSQL column and table identifiers."""
        if not identifier:
            return ""

        if " as " in identifier.lower():
            parts = re.split(r"\s+[aA][sS]\s+", identifier, maxsplit=1)
            raw_col = parts[0].strip().strip('"')
            alias = parts[1].strip().strip('"')
            return f'"{raw_col}" AS "{alias}"'

        clean_id = identifier.strip().strip('"')
        return f'"{clean_id}"'

    @staticmethod
    def _quote_raw_select_stmt(raw_sql: str) -> str:
        """Quotes column names in raw SQL SELECT statements to prevent SQL keyword clashes."""
        if not raw_sql.strip().lower().startswith("select "):
            return raw_sql

        try:
            select_part, from_part = re.split(r"\s+[fF][rR][oO][mM]\s+", raw_sql, maxsplit=1)
            cols_str = select_part[7:].strip()

            quoted_cols = []
            seen_cols = set()

            for col in cols_str.split(","):
                col = col.strip()
                if not col:
                    continue

                if " as " in col.lower():
                    parts = re.split(r"\s+[aA][sS]\s+", col, maxsplit=1)
                    raw_col = parts[0].strip().strip('"')
                    alias = parts[1].strip().strip('"')

                    if alias in seen_cols:
                        continue
                    seen_cols.add(alias)
                    quoted_cols.append(f'"{raw_col}" AS "{alias}"')

                elif not col.startswith('"') and not col.endswith('"') and "(" not in col:
                    clean_col = col.strip('"')
                    if clean_col in seen_cols:
                        continue
                    seen_cols.add(clean_col)
                    quoted_cols.append(f'"{clean_col}"')

                else:
                    if col in seen_cols:
                        continue
                    seen_cols.add(col)
                    quoted_cols.append(col)

            return f"SELECT {', '.join(quoted_cols)} FROM {from_part}"
        except Exception:
            return raw_sql

    @staticmethod
    def _compile_stmt(stmt: Union[str, Select]) -> str:
        """Utility to compile a SQLAlchemy Select or raw string into SQL text."""
        if isinstance(stmt, str):
            return stmt
        return str(stmt.compile(compile_kwargs={"literal_binds": True}))

    @staticmethod
    def _build_mvt_sql(
        stmt: Union[str, Select],
        z: int,
        x: int,
        y: int,
        geom_col: str = "geom",
        layer_name: str = "default_layer",
        buffer: int = 64,
        extent: int = 4096,
        resolution: float = 0
    ) -> str:
        """Constructs unified PostGIS ST_AsMVT SQL string."""
        raw_sql = DBQuery._compile_stmt(stmt)
        sanitized_sql = DBQuery._quote_raw_select_stmt(raw_sql)
        clean_geom = geom_col.replace('"', '').strip()
        geom_ref = f't."{clean_geom}"'

        geom_expression = f"ST_Simplify({geom_ref}, {resolution})" if resolution > 0 else geom_ref

        return f"""
            WITH 
            bounds AS (
              SELECT ST_TileEnvelope({z}, {x}, {y}) AS envelope
            ),
            subquery AS (
              {sanitized_sql}
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
              FROM subquery AS t, bounds
              WHERE ST_Intersects(
                  ST_Transform({geom_ref}, 3857), 
                  bounds.envelope
              )
            )
            SELECT ST_AsMVT(
                mvt_geoms, 
                '{layer_name}', 
                {extent}, 
                'mvt_geom'
            ) 
            FROM mvt_geoms;
        """

    @staticmethod
    def _build_geojson_sql(
        stmt: Union[str, Select],
        geom_col: str = "geom",
        id_col: str = "id"
    ) -> str:
        """Constructs unified PostGIS GeoJSON FeatureCollection SQL string."""
        raw_sql = DBQuery._compile_stmt(stmt)
        clean_geom = geom_col.replace('"', '').strip()
        clean_id = id_col.replace('"', '').strip()

        return f"""
            SELECT jsonb_build_object(
                'type', 'FeatureCollection',
                'crs', jsonb_build_object('type', 'name', 'properties', jsonb_build_object('name', 'EPSG:4326')),
                'features', COALESCE(jsonb_agg(features.feature), '[]'::jsonb)
            )
            FROM (
                SELECT jsonb_build_object(
                    'type', 'Feature',
                    'id', inputs."{clean_id}",
                    'geometry', ST_AsGeoJSON((inputs."{clean_geom}")::geometry)::jsonb,
                    'properties', to_jsonb(inputs) - '{clean_geom}'
                ) AS feature
                FROM ({raw_sql}) inputs
                WHERE inputs."{clean_geom}" IS NOT NULL
            ) features;
        """

    # =========================================================================
    # MVT EXECUTORS
    # =========================================================================

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
        resolution: float = 0
    ) -> bytes:
        """Generates Mapbox Vector Tile (MVT) binary protocol buffer data via PostGIS."""
        if session.get_bind().dialect.name != "postgresql":
            raise NotImplementedError("MVT generation is only supported on PostgreSQL/PostGIS.")

        mvt_sql = DBQuery._build_mvt_sql(
            stmt, z, x, y, geom_col, layer_name, buffer, extent, resolution
        )

        try:
            result = session.execute(text(mvt_sql)).scalar()
            return bytes(result) if result else b""
        except Exception:
            da_logger.exception("Error during MVT generation")
            return b""

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
        extent: int = 4096,
        resolution: float = 0
    ) -> bytes:
        """Async generation of Mapbox Vector Tile (MVT) binary protocol buffer data via PostGIS."""
        if session.get_bind().dialect.name != "postgresql":
            raise NotImplementedError("MVT generation is only supported on PostgreSQL/PostGIS.")

        mvt_sql = DBQuery._build_mvt_sql(
            stmt, z, x, y, geom_col, layer_name, buffer, extent, resolution
        )

        try:
            result = await session.execute(text(mvt_sql))
            tile = result.scalar()
            return bytes(tile) if tile else b""
        except Exception:
            da_logger.exception("Error during async MVT generation")
            return b""

    # =========================================================================
    # GEOJSON EXECUTORS
    # =========================================================================

    @staticmethod
    def execute_query_as_geojson(
        session: Session,
        stmt: Union[str, Select],
        geom_col: str = "geom",
        id_col: str = "id",
    ) -> Dict[str, Any]:
        """Builds a GeoJSON FeatureCollection synchronously."""
        engine = session.get_bind()

        if engine.dialect.name == "postgresql":
            pg_geojson_sql = DBQuery._build_geojson_sql(stmt, geom_col, id_col)
            result = session.execute(text(pg_geojson_sql)).scalar()
            return result if result else {"type": "FeatureCollection", "features": []}

        # Fallback for non-PostgreSQL dialects
        result = session.execute(text(stmt) if isinstance(stmt, str) else stmt)
        features = []
        for row in result.mappings():
            geom = row.get(geom_col)
            if geom is None:
                continue
            features.append({
                "type": "Feature",
                "id": row.get(id_col),
                "geometry": json.loads(geom) if isinstance(geom, str) else geom,
                "properties": {k: v for k, v in row.items() if k != geom_col},
            })
        return {"type": "FeatureCollection", "features": features}

    @staticmethod
    async def execute_query_as_geojson_async(
        session: AsyncSession,
        stmt: Union[str, Select],
        geom_col: str = "geom",
        id_col: str = "id"
    ) -> Dict[str, Any]:
        """Async high-speed GeoJSON builder for PostGIS / fallback dialects."""
        engine = session.get_bind()

        if engine.dialect.name == "postgresql":
            pg_geojson_sql = DBQuery._build_geojson_sql(stmt, geom_col, id_col)
            result = await session.execute(text(pg_geojson_sql))
            return result.scalar() or {"type": "FeatureCollection", "features": []}

        result = await session.execute(text(stmt) if isinstance(stmt, str) else stmt)
        features = []
        for row in result.mappings():
            geom = row.get(geom_col)
            if not geom:
                continue
            features.append({
                "type": "Feature",
                "id": row.get(id_col),
                "geometry": json.loads(geom) if isinstance(geom, str) else geom,
                "properties": {k: v for k, v in row.items() if k != geom_col}
            })
        return {"type": "FeatureCollection", "features": features}

    # =========================================================================
    # GEOPANDAS EXECUTOR
    # =========================================================================

    @staticmethod
    def execute_query_as_gdf(
        session: Session,
        stmt: Union[str, Select],
        params: Optional[dict] = None,
        geom_col: str = "geom",
        crs: str = "EPSG:4326"
    ) -> gpd.GeoDataFrame:
        """Executes a spatial query and converts the results into a GeoPandas GeoDataFrame."""
        executable_stmt = text(stmt) if isinstance(stmt, str) else stmt
        rs = session.execute(executable_stmt, params or {})

        df = pd.DataFrame(rs.mappings().all())

        if df.empty:
            return gpd.GeoDataFrame(columns=df.columns)

        if geom_col in df.columns:
            def parse_geom(x):
                if x is None:
                    return None
                if hasattr(x, "data"):
                    return wkb.loads(bytes(x.data))
                if isinstance(x, str):
                    return wkb.loads(x, hex=True)
                if isinstance(x, bytes):
                    return wkb.loads(x)
                return x

            df[geom_col] = df[geom_col].apply(parse_geom)
            return gpd.GeoDataFrame(df, geometry=geom_col, crs=crs)

        raise ValueError(f"Geometry column '{geom_col}' not found in query results.")