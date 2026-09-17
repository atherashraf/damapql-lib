from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from pyproj import Transformer
from shapely.geometry import box
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.damapql.dch.api_enum import DataModel
from app.damapql.dch.models import LayerInfo
from digitalarzengine.io.managers.stac_manager import STACManager
from digitalarzengine.utils.singletons import da_logger


class LayerInfoStacService:
    """
    Coordinates registration and synchronization of DAMapQL LayerInfo
    records with the DuckDB Spatial STAC catalog.
    """

    def __init__(self, stac_mgr: STACManager):
        self.stac_mgr = stac_mgr

    # =========================================================================
    # Helpers
    # =========================================================================

    @staticmethod
    def resolve_collection_id(info: LayerInfo) -> str:
        """Map LayerCategory to STAC Collection ID, defaulting to app_label or 'default'."""
        if info.layer_category:
            return info.layer_category.slug
        return info.app_label or "default"

    @staticmethod
    def resolve_datetime(info: LayerInfo) -> datetime:
        """Extract a valid datetime object for temporal indexing."""
        dt_val = info.temporal_res or info.uploaded_date
        if dt_val is None:
            return datetime.now(timezone.utc)
        if isinstance(dt_val, datetime):
            return dt_val
        # Handle date instances
        return datetime.combine(dt_val, datetime.min.time(), tzinfo=timezone.utc)

    @staticmethod
    def calculate_wgs84_bbox(info: LayerInfo) -> list[float]:
        """Convert LayerInfo native extent into EPSG:4326 [minx, miny, maxx, maxy]."""
        if not info.extent or len(info.extent) != 4:
            raise ValueError(f"Layer '{info.uuid}' has an invalid or missing extent.")

        minx, miny, maxx, maxy = [float(v) for v in info.extent]
        srid = info.srid or 4326

        if srid == 4326:
            return [minx, miny, maxx, maxy]

        transformer = Transformer.from_crs(f"EPSG:{srid}", "EPSG:4326", always_xy=True)
        x1, y1 = transformer.transform(minx, miny)
        x2, y2 = transformer.transform(maxx, maxy)

        return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]

    def build_layer_properties(self, info: LayerInfo) -> dict[str, Any]:
        """Generate standardized STAC metadata properties."""
        dt = self.resolve_datetime(info)
        dt_str = dt.isoformat()
        if dt.tzinfo is None:
            dt_str += "Z"

        props: dict[str, Any] = {
            "title": info.title,
            "datetime": dt_str,
            "damapql:layer_name": info.layer_name,
            "damapql:format": info.format,
            "damapql:srid": info.srid,
            "damapql:data_model": info.data_model.code if info.data_model else None,
            "damapql:model_type": info.model_type.code if info.model_type else None,
            "damapql:model_name": info.model_name,
            "damapql:geom_type": info.geom_type,
            "damapql:quality_level": info.quality_level.name if info.quality_level else None,
            "damapql:uploaded_date": info.uploaded_date.isoformat() if info.uploaded_date else None,
        }

        # Include custom attributes from extra_metadata without collision
        if info.extra_metadata and isinstance(info.extra_metadata, dict):
            for k, v in info.extra_metadata.items():
                if k not in props and not k.startswith("damapql:"):
                    props[f"damapql:{k}"] = v

        return {k: v for k, v in props.items() if v is not None}

    # =========================================================================
    # Modality Registrations
    # =========================================================================

    def register_raster_layer(self, info: LayerInfo) -> bool:
        """Register a MinIO-backed COG layer using COGRaster header inspection."""
        access = info.layer_accessibility
        if not access or not access.is_minio:
            raise ValueError(f"Raster layer '{info.layer_name}' lacks MinIO accessibility settings.")

        bucket = access.s3_bucket
        object_name = access.s3_object_name
        if not bucket or not object_name:
            raise ValueError(f"MinIO bucket or object_name is missing for layer '{info.layer_name}'.")

        collection = self.resolve_collection_id(info)
        dt = self.resolve_datetime(info)
        props = self.build_layer_properties(info)

        return self.stac_mgr.register_cog(
            bucket_name=bucket,
            object_name=object_name,
            collection=collection,
            item_id=str(info.uuid),
            timestamp=dt,
            properties=props,
        )

    def register_vector_layer(self, info: LayerInfo) -> bool:
        """Register a database-backed vector layer footprint directly into DuckDB Spatial."""
        access = info.layer_accessibility
        if not access or not access.is_db:
            raise ValueError(f"Vector layer '{info.layer_name}' is not configured with DB accessibility.")

        collection = self.resolve_collection_id(info)
        item_id = str(info.uuid)
        dt = self.resolve_datetime(info)
        props = self.build_layer_properties(info)

        bbox = self.calculate_wgs84_bbox(info)
        footprint_wkt = box(bbox[0], bbox[1], bbox[2], bbox[3]).wkt
        crs_code = f"EPSG:{info.srid or 4326}"

        uri = info.data_url or f"db://{access.conn_name}/{access.table_name}"
        bucket = "database"
        object_name = access.table_name or info.layer_name

        con = self.stac_mgr._get_connection()
        try:
            con.execute(f"""
                INSERT OR REPLACE INTO {self.stac_mgr.table_name}
                (id, collection, bucket, object_name, uri, datetime, footprint, crs, 
                 res_x, res_y, band_count, dtype, nodata, properties)
                VALUES (?, ?, ?, ?, ?, ?, ST_GeomFromText(?), ?, ?, ?, ?, ?, ?, ?::JSON);
            """, (
                item_id, collection, bucket, object_name, uri,
                dt, footprint_wkt, crs_code, None, None, None,
                "vector", None, json.dumps(props)
            ))
            da_logger.info(f"Registered Vector layer '{item_id}' ({info.layer_name}) into DuckDB STAC.")
            return True
        except Exception as e:
            da_logger.error(f"Failed to index vector layer '{info.layer_name}' into STAC: {e}", exc_info=True)
            return False
        finally:
            con.close()

    # =========================================================================
    # Entry Points
    # =========================================================================

    def register_layer(self, info: LayerInfo, session: Optional[Session] = None) -> bool:
        """
        Detect layer type, register into DuckDB Spatial, and update
        LayerInfo.extra_metadata with STAC indexing audit flags.
        """
        if not info.data_model:
            raise ValueError(f"Layer '{info.layer_name}' is missing a valid data_model.")

        code = getattr(info.data_model, "code", str(info.data_model))
        success = False

        if code in {DataModel.R.name, DataModel.R.value}:
            success = self.register_raster_layer(info)
        elif code in {DataModel.V.name, DataModel.V.value}:
            success = self.register_vector_layer(info)
        else:
            raise ValueError(f"Unsupported data_model '{code}' for STAC indexing.")

        if success:
            extra = dict(info.extra_metadata or {})
            extra["stac_registered"] = True
            extra["stac_registered_at"] = datetime.now(timezone.utc).isoformat()
            extra["stac_collection"] = self.resolve_collection_id(info)
            info.extra_metadata = extra

            if session:
                flag_modified(info, "extra_metadata")
                session.commit()

        return success

    def register_by_uuid(self, uuid: str) -> bool:
        """Fetch LayerInfo by UUID and register it into the STAC catalog."""
        with LayerInfo.managed_session() as session:
            stmt = (
                select(LayerInfo)
                .options(
                    selectinload(LayerInfo.layer_accessibility),
                    selectinload(LayerInfo.layer_category),
                    selectinload(LayerInfo.quality_level),
                )
                .where(LayerInfo.uuid == uuid)
            )
            info = session.execute(stmt).scalar_one_or_none()
            if not info:
                raise ValueError(f"LayerInfo with UUID '{uuid}' not found.")

            return self.register_layer(info, session=session)