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


class VectorStorageEngineType(str, Enum):
    POSTGIS = "postgis"
    FLATGEOBUF = "flatgeobuf"
    GEOPARQUET = "geoparquet"
    GEOPACKAGE = "geopackage"


@dataclass
class VectorIngestResult:
    """Outcome of a successful vector ingestion pipeline."""
    uuid: str
    layer_info_id: int
    layer_access_id: int
    format: str
    engine: VectorStorageEngineType
    feature_count: int
    geom_type: List[str]
    srid: int


@dataclass
class EnginePersistenceResult:
    accessibility_type: AccessibilityType
    format_label: str
    params: Dict[str, Any]
    created_table_name: Optional[str] = None
    created_minio_object: Optional[str] = None
    created_local_file: Optional[str] = None
    # Companion PMTiles tracking for rollback
    created_pmtiles_minio_object: Optional[str] = None
    created_pmtiles_local_file: Optional[str] = None


class BaseVectorStorageEngine(ABC):
    """Abstract Strategy interface for vector persistence engines."""

    engine_type: VectorStorageEngineType

    @abstractmethod
    def persist(self, *, session: Session, gdf: gpd.GeoDataFrame, safe_title: str, uuid_str: str,
            category: LayerCategory, minio_manager: Optional[MinioManager] = None, bucket_name: str,
            target_srid: int = 4326, extra_params: Optional[Dict[str, Any]] = None, ) -> EnginePersistenceResult:
        """Persists the validated GeoDataFrame to its target storage mechanism."""
        pass

    @abstractmethod
    def rollback(self, *, session: Session, persistence_result: EnginePersistenceResult,
            minio_manager: Optional[MinioManager] = None, bucket_name: str, ) -> None:
        """Rolls back partially committed files or database tables upon transaction failure."""
        pass
