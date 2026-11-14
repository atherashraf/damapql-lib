# app/damapql/dch/services/layer_accessibility/vector_access_service.py
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import uuid4

import geopandas as gpd
import pyogrio
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger
from fastapi import HTTPException
from pyarrow.fs import S3FileSystem
from pyproj import CRS, Transformer
from shapely.geometry import box
from sqlalchemy import Engine

from app.damapql import settings
from app.damapql.dch.models import LayerAccessibility, LayerInfo
from app.damapql.settings import MEDIA_BASE


class VectorSourceFormat(str, Enum):
    POSTGIS = "POSTGIS"
    FGB = "FGB"
    GEOPARQUET = "GEOPARQUET"
    GPKG = "GPKG"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VectorTarget:
    """Unified descriptor resolved by VectorAccessService for PostGIS, FGB, GeoParquet, and GPKG."""
    layer_uuid: str
    layer_name: str
    source_format: VectorSourceFormat
    srid: int
    geom_col: str
    columns: list[str]
    pk_cols: list[str]
    feature_count: Optional[int]
    zoom_range: list[int]
    # PostGIS (DB)
    engine: Optional[Engine] = None
    table_name: Optional[str] = None
    pk_sql_fragment: str = '"id" as pk'
    # File / Object Storage (FGB, GeoParquet, GPKG)
    source_uri: Optional[str] = None
    local_path: Optional[Path] = None
    gpkg_layer: Optional[str] = None
    # MinIO / S3 connection details for PyArrow S3FileSystem (GeoParquet)
    s3_bucket: Optional[str] = None
    s3_object_name: Optional[str] = None
    s3_endpoint: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_secure: bool = False


class VectorAccessService:
    """
    Resolve and read a vector dataset based on LayerAccessibility configuration.
            LayerInfo
               ↓
            LayerAccessibility
               ↓
            accessibility_params
               │
               ├── DB (PostGIS)
               │     ↓
               │   Engine + table_name
               │
               ├── MinIO / S3
               │     ├── FGB        ──▶ /vsis3/<bucket>/<object> (GDAL Range Reads)
               │     ├── GeoParquet ──▶ PyArrow S3FileSystem     (Arrow Range Reads)
               │     └── GPKG       ──▶ Local cached SQLite/GPKG file
               │
               └── File
                     ↓
                 MEDIA_BASE / relative path or absolute path
    """

    CACHE_ROOT = Path(tempfile.gettempdir()) / "damap_vector_cache"
    _download_locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()
    _crs_cache: dict[str, CRS] = {}
    _s3_fs_cache: dict[tuple[str, str, str, bool], S3FileSystem] = {}
    _s3_fs_guard = threading.Lock()

    @classmethod
    def _get_object_lock(cls, key: str) -> threading.Lock:
        with cls._locks_guard:
            lock = cls._download_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._download_locks[key] = lock
            return lock

    @classmethod
    def _get_s3_filesystem(
        cls,
        *,
        endpoint: Optional[str],
        access_key: Optional[str],
        secret_key: Optional[str],
        secure: bool,
    ) -> S3FileSystem:
        cache_key = (endpoint or "", access_key or "", secret_key or "", secure)
        cached = cls._s3_fs_cache.get(cache_key)
        if cached is not None:
            return cached

        with cls._s3_fs_guard:
            cached = cls._s3_fs_cache.get(cache_key)
            if cached is not None:
                return cached

            fs = S3FileSystem(
                access_key=access_key,
                secret_key=secret_key,
                endpoint_override=endpoint,
                scheme="https" if secure else "http",
            )
            cls._s3_fs_cache[cache_key] = fs
            return fs

    @staticmethod
    def configure_vsis3(minio_manager: MinioManager) -> None:
        """Configures GDAL /vsis3/ environment variables for MinIO HTTP range requests."""
        access_key = getattr(settings, "MINIO_ACCESS_KEY", None) or getattr(settings, "AWS_ACCESS_KEY_ID", "")
        secret_key = getattr(settings, "MINIO_SECRET_KEY", None) or getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        secure = bool(getattr(settings, "MINIO_SECURE", False))

        os.environ["AWS_ACCESS_KEY_ID"] = access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
        os.environ["AWS_S3_ENDPOINT"] = f"{minio_manager.host}:{minio_manager.port}"
        os.environ["AWS_HTTPS"] = "YES" if secure else "NO"
        os.environ["AWS_VIRTUAL_HOSTING"] = "FALSE"
        os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"

    @staticmethod
    def extract_column_names(access: Optional[LayerAccessibility]) -> list[str]:
        if not access:
            return []
        cols = (access.params or {}).get("columns") or []
        if isinstance(cols, str):
            try:
                cols = json.loads(cols)
            except Exception:
                return []

        names: list[str] = []
        for col in cols:
            if isinstance(col, dict) and col.get("name"):
                names.append(col["name"])
            elif isinstance(col, str) and col:
                names.append(col)
        return names

    @staticmethod
    def extract_column_schema(access: Optional[LayerAccessibility]) -> list[dict[str, str]]:
        if not access:
            return []
        cols = (access.params or {}).get("columns") or []
        if isinstance(cols, str):
            try:
                cols = json.loads(cols)
            except Exception:
                return []

        schema: list[dict[str, str]] = []
        for col in cols:
            if isinstance(col, dict) and col.get("name"):
                schema.append({"name": col["name"], "d_type": col.get("d_type", "string")})
            elif isinstance(col, str) and col:
                schema.append({"name": col, "d_type": "string"})
        return schema

    @classmethod
    def detect_format(cls, info: LayerInfo, access: LayerAccessibility) -> VectorSourceFormat:
        if access.is_db:
            return VectorSourceFormat.POSTGIS

        fmt = (info.format or "").strip().upper()
        raw_params = access.params or {}
        path_hint = (
            access.s3_object_name
            or raw_params.get("object_name", "")
            or raw_params.get("file_path", "")
            or str(access.file_path or "")
        ).lower()

        if fmt in ("FGB", "FLATGEOBUF") or path_hint.endswith(".fgb"):
            return VectorSourceFormat.FGB
        if fmt in ("PARQUET", "GEOPARQUET") or path_hint.endswith((".parquet", ".geoparquet")):
            return VectorSourceFormat.GEOPARQUET
        if fmt in ("GPKG", "GEOPACKAGE") or path_hint.endswith(".gpkg"):
            return VectorSourceFormat.GPKG

        return VectorSourceFormat.UNKNOWN

    @classmethod
    def get_cached_object_path(cls, bucket: str, object_name: str) -> Path:
        safe_parts = [p for p in object_name.replace("\\", "/").split("/") if p and p not in (".", "..")]
        return cls.CACHE_ROOT.joinpath(bucket, *safe_parts)

    @classmethod
    def resolve_local_file(
        cls,
        access: LayerAccessibility,
        minio_manager: MinioManager | None = None,
    ) -> Path:
        """Downloads and caches MinIO/S3 objects locally (used for GPKG or explicit local file needs)."""
        params = access.accessibility_params

        if access.is_minio or access.is_s3:
            if minio_manager is None:
                raise HTTPException(status_code=503, detail="MinIO service is unavailable")

            bucket = getattr(params, "bucket", None) or access.s3_bucket
            object_name = getattr(params, "object_name", None) or access.s3_object_name
            if not bucket or not object_name:
                raise HTTPException(status_code=404, detail="Object storage bucket/object_name missing")

            local_path = cls.get_cached_object_path(bucket, object_name)
            if local_path.exists() and local_path.stat().st_size > 0:
                return local_path

            lock = cls._get_object_lock(f"{bucket}/{object_name}")
            with lock:
                if local_path.exists() and local_path.stat().st_size > 0:
                    return local_path

                local_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = local_path.with_name(f"{local_path.name}.{uuid4().hex}.tmp")
                try:
                    ok = minio_manager.download_file(
                        bucket_name=bucket,
                        object_name=object_name,
                        destination_path=str(tmp_path),
                    )
                    if not ok or not tmp_path.exists():
                        raise HTTPException(
                            status_code=404,
                            detail=f"Failed to download vector object '{object_name}' from bucket '{bucket}'",
                        )
                    os.replace(tmp_path, local_path)
                finally:
                    tmp_path.unlink(missing_ok=True)

            return local_path

        if access.is_file:
            raw_fp = getattr(params, "file_path", None)
            if not raw_fp:
                raise HTTPException(status_code=404, detail="Layer file path missing")

            is_media_rel = (
                getattr(params, "is_path_media_rel", False)
                or getattr(params, "path_base", None) == "media_rel"
            )
            if is_media_rel:
                media_root = Path(MEDIA_BASE).resolve()
                rel_parts = [p for p in str(raw_fp).replace("\\", "/").split("/") if p and p != "."]
                file_path = (media_root.joinpath(*rel_parts)).resolve()
                if not file_path.is_relative_to(media_root):
                    raise HTTPException(status_code=400, detail="Invalid media-relative vector path")
            else:
                file_path = Path(raw_fp)

            if not file_path.exists():
                raise HTTPException(status_code=404, detail=f"Vector file not found: {file_path}")
            return file_path

        raise HTTPException(
            status_code=400,
            detail=f"Unsupported vector accessibility type: {access.accessibility_type}",
        )

    @classmethod
    def _evict_crs_prefix(cls, source_prefix: str) -> None:
        stale_keys = [k for k in cls._crs_cache if k.startswith(f"{source_prefix}::")]
        for k in stale_keys:
            cls._crs_cache.pop(k, None)

    @classmethod
    def evict_cached_file(cls, info: Optional[LayerInfo]) -> bool:
        """Evicts locally cached MinIO/S3 vector files and CRS metadata cache on clear/reconcile."""
        access = info.layer_accessibility if info else None
        if not access:
            return False

        if (access.is_minio or access.is_s3) and access.s3_bucket and access.s3_object_name:
            vsi_uri = f"/vsis3/{access.s3_bucket}/{access.s3_object_name.lstrip('/')}"
            cls._evict_crs_prefix(vsi_uri)

            cached = cls.get_cached_object_path(access.s3_bucket, access.s3_object_name)
            cls._evict_crs_prefix(str(cached))
            if cached.exists():
                cached.unlink(missing_ok=True)
                da_logger.info(f"Evicted local vector cache file: {cached}")
                return True
        elif access.is_file and access.file_path:
            cls._evict_crs_prefix(str(access.file_path))

        return False

    @classmethod
    def resolve(
        cls,
        info: LayerInfo,
        minio_manager: MinioManager | None = None,
    ) -> VectorTarget:
        access = info.layer_accessibility
        if not access:
            raise HTTPException(status_code=404, detail="Layer accessibility configuration missing")

        raw_params = access.params or {}
        if not raw_params:
            raise HTTPException(status_code=404, detail="Layer accessibility parameters missing")

        source_fmt = cls.detect_format(info, access)
        columns = cls.extract_column_names(access)
        srid = info.srid or 4326
        geom_col = (
            access.geometry_col
            or raw_params.get("geometry_col")
            or raw_params.get("primary_geometry_column")
            or "geom"
        )
        feature_count = getattr(info, "total_rows", None) or raw_params.get("feature_count")
        zoom_range = info.zoom_range or [0, 30]
        layer_name = info.layer_name or info.uuid

        # 1. PostGIS Database
        if source_fmt == VectorSourceFormat.POSTGIS:
            engine = access.get_external_engine()
            if not engine or not access.table_name:
                raise HTTPException(status_code=404, detail="Unable to resolve PostGIS database target")

            return VectorTarget(
                layer_uuid=info.uuid,
                layer_name=layer_name,
                source_format=source_fmt,
                srid=srid,
                geom_col=geom_col,
                columns=columns,
                pk_cols=access.pk_cols or ["dch_id"],
                feature_count=feature_count,
                zoom_range=zoom_range,
                engine=engine,
                table_name=access.table_name,
                pk_sql_fragment=info.pk_cols_as_pk,
            )

        # 2. MinIO / S3 Direct Streaming for FlatGeobuf & GeoParquet
        if (access.is_minio or access.is_s3) and source_fmt in (
            VectorSourceFormat.FGB,
            VectorSourceFormat.GEOPARQUET,
        ):
            if minio_manager is None:
                raise HTTPException(status_code=503, detail="MinIO service is unavailable")

            params = access.accessibility_params
            bucket = getattr(params, "bucket", None) or access.s3_bucket
            object_name = getattr(params, "object_name", None) or access.s3_object_name
            if not bucket or not object_name:
                raise HTTPException(status_code=404, detail="Object storage bucket/object_name missing")

            cls.configure_vsis3(minio_manager)
            vsi_uri = f"/vsis3/{bucket}/{object_name.lstrip('/')}"

            access_key = getattr(settings, "MINIO_ACCESS_KEY", None) or getattr(settings, "AWS_ACCESS_KEY_ID", "")
            secret_key = getattr(settings, "MINIO_SECRET_KEY", None) or getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
            secure = bool(getattr(settings, "MINIO_SECURE", False))

            return VectorTarget(
                layer_uuid=info.uuid,
                layer_name=layer_name,
                source_format=source_fmt,
                srid=srid,
                geom_col=geom_col,
                columns=columns,
                pk_cols=access.pk_cols or [],
                feature_count=feature_count,
                zoom_range=zoom_range,
                source_uri=vsi_uri,
                s3_bucket=bucket,
                s3_object_name=object_name.lstrip("/"),
                s3_endpoint=f"{minio_manager.host}:{minio_manager.port}",
                s3_access_key=access_key,
                s3_secret_key=secret_key,
                s3_secure=secure,
            )

        # 3. Local Disk File or Cached GeoPackage
        local_path = cls.resolve_local_file(access, minio_manager=minio_manager)
        return VectorTarget(
            layer_uuid=info.uuid,
            layer_name=layer_name,
            source_format=source_fmt,
            srid=srid,
            geom_col=geom_col,
            columns=columns,
            pk_cols=access.pk_cols or [],
            feature_count=feature_count,
            zoom_range=zoom_range,
            source_uri=str(local_path),
            local_path=local_path,
            gpkg_layer=raw_params.get("layer_name") or raw_params.get("table_name"),
        )

    @classmethod
    def _detect_file_crs(
        cls,
        target: VectorTarget,
        source: str,
    ) -> CRS:
        cache_key = f"{source}::{target.gpkg_layer or ''}"
        cached = cls._crs_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            if target.source_format == VectorSourceFormat.GEOPARQUET and target.s3_bucket and target.s3_object_name:
                import json as _json
                import pyarrow.parquet as _pq

                s3_fs = cls._get_s3_filesystem(
                    endpoint=target.s3_endpoint,
                    access_key=target.s3_access_key,
                    secret_key=target.s3_secret_key,
                    secure=target.s3_secure,
                )
                schema = _pq.read_schema(f"{target.s3_bucket}/{target.s3_object_name}", filesystem=s3_fs)
                geo_raw = (schema.metadata or {}).get(b"geo")
                if geo_raw:
                    geo_meta = _json.loads(geo_raw.decode("utf-8"))
                    col_meta = (geo_meta.get("columns") or {}).get(target.geom_col, {})
                    if col_meta.get("crs"):
                        resolved = CRS.from_user_input(col_meta["crs"])
                        cls._crs_cache[cache_key] = resolved
                        return resolved

            if target.source_format in (VectorSourceFormat.FGB, VectorSourceFormat.GPKG, VectorSourceFormat.GEOPARQUET):
                kwargs = (
                    {"layer": target.gpkg_layer}
                    if (target.source_format == VectorSourceFormat.GPKG and target.gpkg_layer)
                    else {}
                )
                meta = pyogrio.read_info(source, **kwargs)
                if meta.get("crs"):
                    resolved = CRS.from_user_input(meta["crs"])
                    cls._crs_cache[cache_key] = resolved
                    return resolved

        except Exception as err:
            da_logger.warning(f"[VectorAccessService] Could not inspect CRS from {source}: {err}")

        fallback = CRS.from_epsg(target.srid or 4326)
        cls._crs_cache[cache_key] = fallback
        return fallback

    @classmethod
    def read_gdf(
        cls,
        target: VectorTarget,
        bbox_4326: Optional[tuple[float, float, float, float]] = None,
        requested_cols: Optional[Sequence[str]] = None,
    ) -> gpd.GeoDataFrame:
        """Reads a full or spatially-filtered GeoDataFrame from S3/MinIO or local path."""
        source = target.source_uri or (str(target.local_path) if target.local_path else None)
        if not source:
            raise ValueError("VectorTarget has no source_uri or local_path for reading.")

        native_crs = cls._detect_file_crs(target=target, source=source)

        read_bbox = None
        if bbox_4326 is not None:
            west, south, east, north = bbox_4326
            if native_crs.to_epsg() == 4326:
                read_bbox = (west, south, east, north)
            else:
                transformer = Transformer.from_crs("EPSG:4326", native_crs, always_xy=True)
                minx, miny = transformer.transform(west, south)
                maxx, maxy = transformer.transform(east, north)
                read_bbox = (minx, miny, maxx, maxy)

        # Push down column selection to pyogrio/Arrow when specific columns are requested
        pushdown_cols: Optional[list[str]] = None
        if requested_cols is not None:
            allowed_set = set(target.columns)
            needed = set(requested_cols).union(target.pk_cols)
            pushdown_cols = [c for c in needed if c in allowed_set]

        if target.source_format == VectorSourceFormat.FGB:
            gdf = gpd.read_file(source, bbox=read_bbox, columns=pushdown_cols, engine="pyogrio")

        elif target.source_format == VectorSourceFormat.GPKG:
            kwargs: dict[str, Any] = {"bbox": read_bbox, "columns": pushdown_cols, "engine": "pyogrio"}
            if target.gpkg_layer:
                kwargs["layer"] = target.gpkg_layer
            gdf = gpd.read_file(source, **kwargs)

        elif target.source_format == VectorSourceFormat.GEOPARQUET:
            pq_cols = ([target.geom_col] + pushdown_cols) if pushdown_cols is not None else None

            if target.s3_bucket and target.s3_object_name:
                s3_fs = cls._get_s3_filesystem(
                    endpoint=target.s3_endpoint,
                    access_key=target.s3_access_key,
                    secret_key=target.s3_secret_key,
                    secure=target.s3_secure,
                )
                pq_path = f"{target.s3_bucket}/{target.s3_object_name}"
                gdf = gpd.read_parquet(pq_path, filesystem=s3_fs, columns=pq_cols)
            else:
                gdf = gpd.read_parquet(source, columns=pq_cols)

            if read_bbox is not None and not gdf.empty:
                gdf = gdf[gdf.geometry.intersects(box(*read_bbox))].copy()

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file vector format: {target.source_format}",
            )

        if gdf.empty:
            return gdf

        if gdf.crs is None:
            gdf = gdf.set_crs(native_crs)

        geom_name = gdf.geometry.name
        if "pk" not in gdf.columns:
            valid_pks = [c for c in target.pk_cols if c in gdf.columns]
            if valid_pks:
                gdf["pk"] = gdf[valid_pks].astype(str).agg("-".join, axis=1)
            else:
                gdf["pk"] = (gdf.index + 1).astype(str)

        if requested_cols is not None:
            keep = ["pk"] + [c for c in requested_cols if c in gdf.columns and c not in (geom_name, "pk")]
            gdf = gdf[keep + [geom_name]]

        return gdf