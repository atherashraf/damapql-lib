# app/damapql/dch/services/ingestion/vector/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import geopandas as gpd
from digitalarzengine.io.managers.minio_manager import MinioManager
from sqlalchemy.orm import Session

from app.damapql.dch.models import AccessibilityType, LayerCategory
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils


class VectorStorageEngineType(str, Enum):
    POSTGIS = "postgis"
    FLATGEOBUF = "flatgeobuf"
    GEOPARQUET = "geoparquet"
    GEOPACKAGE = "geopackage"


@dataclass
class EnginePersistenceResult:
    accessibility_type: AccessibilityType
    format_label: str
    params: Dict[str, Any]
    created_table_name: Optional[str] = None
    created_schema_name: str = "public"
    created_db_key: Optional[str] = None
    created_minio_object: Optional[str] = None
    created_local_file: Optional[str] = None


@dataclass
class VectorPersistSummary:
    """Generic persistence output usable by LayerInfo, TempLayerInfo, or custom routers."""
    uuid: str
    layer_name: str
    format: str
    engine: VectorStorageEngineType
    accessibility_type: AccessibilityType
    access_params: Dict[str, Any]
    feature_count: int
    geom_type: List[str]
    primary_geom_type: str
    srid: int
    extent: List[float]
    extent_3857: Optional[List[float]]
    persistence_result: EnginePersistenceResult


@dataclass
class VectorIngestResult:
    """Outcome of a successful vector ingestion pipeline into LayerInfo."""
    uuid: str
    layer_info_id: int
    layer_access_id: int
    format: str
    engine: VectorStorageEngineType
    feature_count: int
    geom_type: List[str]
    srid: int
    table_name: Optional[str] = None


def build_layer_columns_meta(gdf: gpd.GeoDataFrame, geom_col: str = "geom") -> List[Dict[str, str]]:
    """Builds LayerColumnParam-compatible metadata matching LayerAccessibility.cols_name."""
    cols: List[Dict[str, str]] = []
    for col_name, dtype in gdf.dtypes.items():
        if col_name == geom_col:
            continue
        cols.append({
            "name": str(col_name),
            "d_type": ManageDataFrameUtils.getColGenericType(str(dtype).lower()),
        })
    return cols


class BaseVectorStorageEngine(ABC):
    """Abstract Strategy interface for vector persistence engines."""

    engine_type: VectorStorageEngineType

    @abstractmethod
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
        """Persists the validated GeoDataFrame to its target storage mechanism."""
        pass

    @abstractmethod
    def rollback(
        self,
        *,
        session: Session,
        persistence_result: EnginePersistenceResult,
        minio_manager: Optional[MinioManager] = None,
        bucket_name: str = "dch",
    ) -> None:
        """Rolls back partially committed files or database tables upon transaction failure."""
        pass