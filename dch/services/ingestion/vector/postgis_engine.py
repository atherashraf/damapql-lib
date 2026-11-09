# app/damapql/dch/services/ingestion/vector/postgis_engine.py
from __future__ import annotations

from typing import Any, Dict, Optional

import geopandas as gpd
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.models import AccessibilityType, LayerCategory
from app.damapql.dch.services.ingestion.vector.base import (BaseVectorStorageEngine, EnginePersistenceResult,
                                                            VectorStorageEngineType, )
from app.damapql.utils.adapters.db import DBBase


class PostgisVectorEngine(BaseVectorStorageEngine):
    engine_type = VectorStorageEngineType.POSTGIS

    def persist(self, *, session: Session, gdf: gpd.GeoDataFrame, safe_title: str, uuid_str: str,
            category: LayerCategory, minio_manager: Optional[MinioManager] = None, bucket_name: str,
            target_srid: int = 4326, extra_params: Optional[Dict[str, Any]] = None, ) -> EnginePersistenceResult:
        clean_uuid = uuid_str.replace("-", "_")
        table_name = f"vec_{safe_title[:28]}_{clean_uuid[:18]}"
        schema_name = (extra_params or {}).get("schema_name", "public")
        db_key = (extra_params or {}).get("db_key", "data")

        # Resolve target engine from multi-database config
        db_engine = DBBase.get_engine(db_key=db_key) if hasattr(DBBase, "get_engine") else session.get_bind()

        db_gdf = gdf.copy()
        if db_gdf.crs and db_gdf.crs.to_epsg() != target_srid:
            db_gdf = db_gdf.to_crs(epsg=target_srid)

        geom_col = db_gdf.geometry.name
        db_gdf.columns = [
            "".join(c if c.isalnum() else "_" for c in col.lower()).strip("_") if col != geom_col else "geom" for col in
            db_gdf.columns]

        db_gdf.to_postgis(name=table_name, con=db_engine, schema=schema_name, if_exists="fail", index=True,
            index_label="fid", )

        idx_name = f"idx_{table_name[:48]}_geom"
        with db_engine.begin() as conn:
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                              f'ON "{schema_name}"."{table_name}" USING GIST ("geom");'))

        accessibility_type = getattr(AccessibilityType, "Database", AccessibilityType.File)
        params = {"table_name": table_name, "schema_name": schema_name, "geom_column": "geom", "srid": target_srid,
            "feature_count": len(db_gdf), "db_key": db_key, }

        return EnginePersistenceResult(accessibility_type=accessibility_type, format_label="PostGIS", params=params,
            created_table_name=table_name, )

    def rollback(self, *, session: Session, persistence_result: EnginePersistenceResult,
            minio_manager: Optional[MinioManager] = None, bucket_name: str, ) -> None:
        table_name = persistence_result.created_table_name
        if not table_name:
            return
        schema_name = persistence_result.params.get("schema_name", "public")
        db_key = persistence_result.params.get("db_key", "data")
        try:
            db_engine = DBBase.get_engine(db_key=db_key) if hasattr(DBBase, "get_engine") else session.get_bind()
            with db_engine.begin() as conn:
                conn.execute(text(f'DROP TABLE IF EXISTS "{schema_name}"."{table_name}" CASCADE;'))
            da_logger.info(f"Rollback: Dropped PostGIS table {schema_name}.{table_name}")
        except Exception as e:
            da_logger.warning(f"Failed to drop PostGIS table {table_name}: {e}")
