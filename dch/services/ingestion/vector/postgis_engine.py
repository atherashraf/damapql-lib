# app/damapql/dch/services/ingestion/vector/postgis_engine.py
from __future__ import annotations

from typing import Any, Dict, Optional

import geopandas as gpd
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import transform as shapely_transform, unary_union
from shapely.validation import make_valid
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.damapql.dch.models import AccessibilityType, LayerCategory, DBConnection
from app.damapql.dch.services.ingestion.vector.base import (
    BaseVectorStorageEngine,
    EnginePersistenceResult,
    VectorStorageEngineType,
    build_layer_columns_meta,
)
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.settings import DCH_DATA_DB_KEY
from app.damapql.utils.adapters.deps import get_sync_engine_instance


class PostgisVectorEngine(BaseVectorStorageEngine):
    engine_type = VectorStorageEngineType.POSTGIS

    @staticmethod
    def _resolve_table_name(
        db_engine,
        explicit_table_name: Optional[str],
        fallback_stem: str,
        schema_name: str = "public",
    ) -> str:
        with db_engine.connect() as conn:
            inspector = inspect(conn)
            existing = {t.lower() for t in inspector.get_table_names(schema=schema_name)}

        # Case 1: Frontend / caller explicitly supplied a table_name
        if explicit_table_name and explicit_table_name.strip():
            normalized = DCHUtils.format_table_name(explicit_table_name.strip(), max_len=54)
            if normalized.lower() in existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Table '{normalized}' already exists in schema '{schema_name}'.",
                )
            return normalized

        # Case 2: No explicit table name provided (e.g. pipeline batch ingestion)
        return DCHUtils.short_table_name(fallback_stem, max_len=54)

    @staticmethod
    def _sanitize_geometry(geom):
        """Strips Z/M dimensions, repairs invalid rings, and unwraps GeometryCollections."""
        if geom is None or geom.is_empty:
            return None

        geom_2d = shapely_transform(lambda x, y, *_: (x, y), geom)
        if not geom_2d.is_valid:
            geom_2d = make_valid(geom_2d)

        if isinstance(geom_2d, GeometryCollection):
            polys = [g for g in geom_2d.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            lines = [g for g in geom_2d.geoms if g.geom_type in ("LineString", "MultiLineString")]
            pts = [g for g in geom_2d.geoms if g.geom_type in ("Point", "MultiPoint")]
            chosen = polys or lines or pts
            return unary_union(chosen) if chosen else None

        return geom_2d

    def persist(
            self,
            *,
            session: Session,
            gdf: gpd.GeoDataFrame,
            title: str,
            safe_title: str,
            uuid_str: str,
            category: Optional[LayerCategory] = None,
            model_type: Optional[str] = None,
            minio_manager: Optional[MinioManager] = None,
            bucket_name: str = "dch",
            target_srid: int = 4326,
            extra_params: Optional[Dict[str, Any]] = None,
    ) -> EnginePersistenceResult:
        extra = extra_params or {}
        schema_name = extra.get("schema_name", "public")
        db_key = extra.get("db_key") or DCH_DATA_DB_KEY

        # 1. Resolve or auto-register DBConnection in 'dch' from settings.DATABASES[db_key]
        conn_name = extra.get("conn_name")
        if not conn_name:
            db_conn = DBConnection.get_or_create_from_db_key(db_key=db_key)
            conn_name = db_conn.name

        # 2. Target spatial database engine ('data')
        db_engine = get_sync_engine_instance(key=db_key)

        table_name = self._resolve_table_name(
            db_engine=db_engine,
            explicit_table_name=extra.get("table_name"),
            fallback_stem=safe_title,
            schema_name=schema_name,
        )

        # 3. Normalize CRS, column names, and geometry column
        db_gdf = gdf.copy()
        if db_gdf.crs and db_gdf.crs.to_epsg() != target_srid:
            db_gdf = db_gdf.to_crs(epsg=target_srid)

        geom_col = db_gdf.geometry.name
        seen_cols: set[str] = {"geom"}
        sanitized_cols: list[str] = []

        for col in db_gdf.columns:
            if col == geom_col:
                sanitized_cols.append("geom")
                continue

            clean = "".join(c if c.isalnum() else "_" for c in col.lower()).strip("_") or "col"
            if clean[0].isdigit():
                clean = f"c_{clean}"

            base = clean
            counter = 1
            while clean in seen_cols:
                clean = f"{base}_{counter}"
                counter += 1

            seen_cols.add(clean)
            sanitized_cols.append(clean)

        db_gdf.columns = sanitized_cols
        db_gdf = db_gdf.set_geometry("geom")

        cols_to_drop = [c for c in ["objectid", "fid", "id", "dch_id"] if c in db_gdf.columns]
        if cols_to_drop:
            db_gdf.drop(columns=cols_to_drop, inplace=True)

        # 4. Strip Z/M coordinates, repair invalid rings, and normalize Polygon -> MultiPolygon
        db_gdf["geom"] = db_gdf["geom"].apply(self._sanitize_geometry)
        db_gdf = db_gdf[db_gdf["geom"].notnull() & ~db_gdf["geom"].is_empty].copy()

        if db_gdf.empty:
            raise ValueError("No valid non-empty geometries remained after geometry sanitization.")

        geom_types = set(db_gdf["geom"].geom_type.unique())
        if geom_types <= {"Polygon", "MultiPolygon"} and "MultiPolygon" in geom_types:
            db_gdf["geom"] = db_gdf["geom"].apply(
                lambda g: MultiPolygon([g]) if isinstance(g, Polygon) else g
            )

        db_gdf = db_gdf.set_geometry("geom")

        # 5. Write table to PostGIS and add dch_id SERIAL PRIMARY KEY + GiST spatial index
        db_gdf.to_postgis(
            name=table_name,
            con=db_engine,
            schema=schema_name,
            if_exists="fail",
            index=False,
        )

        try:
            idx_name = f"idx_{table_name[:48]}_geom"
            with db_engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{schema_name}"."{table_name}" ADD COLUMN dch_id SERIAL PRIMARY KEY;')
                )
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                        f'ON "{schema_name}"."{table_name}" USING GIST ("geom");'
                    )
                )
        except Exception:
            try:
                with db_engine.begin() as conn:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}" CASCADE;'))
            except Exception as drop_err:
                da_logger.warning(f"Failed to clean up table {schema_name}.{table_name} after DDL error: {drop_err}")
            raise

        # 6. Build DBAccessibilityParams-compatible metadata
        columns_meta = build_layer_columns_meta(db_gdf, geom_col="geom")
        columns_meta.insert(0, {"name": "dch_id", "d_type": "number"})

        params: Dict[str, Any] = {
            "type": "db",
            "conn_name": conn_name,
            "table_name": table_name,
            "columns": columns_meta,
            "g_col": "geom",
            "pk_cols": ["dch_id"],
        }

        return EnginePersistenceResult(
            accessibility_type=AccessibilityType.DB,
            format_label="MVT",
            params=params,
            created_table_name=table_name,
            created_schema_name=schema_name,
            created_db_key=db_key,
        )

    def rollback(
        self,
        *,
        session: Session,
        persistence_result: EnginePersistenceResult,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = "dch",
    ) -> None:
        table_name = persistence_result.created_table_name
        if not table_name:
            return
        schema_name = persistence_result.created_schema_name or "public"
        db_key = persistence_result.created_db_key or DCH_DATA_DB_KEY
        try:
            db_engine = get_sync_engine_instance(key=db_key)
            with db_engine.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}" CASCADE;'))
            da_logger.info(f"Rollback: Dropped PostGIS table {schema_name}.{table_name} from '{db_key}'")
        except Exception as e:
            da_logger.warning(f"Failed to drop PostGIS table {table_name} from '{db_key}': {e}")