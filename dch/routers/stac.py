from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from pyproj import Transformer
from shapely.geometry import box, mapping
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.damapql.dch.models import LayerInfo
from app.damapql.utils.adapters.deps import get_minio_manager
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

# ============================================================================
# Router
# ============================================================================

stac_router = APIRouter(prefix="/stac", tags=["STAC"])

# ============================================================================
# Constants & Conformance for QGIS Native Browser
# ============================================================================

STAC_VERSION = "1.0.0"

# QGIS inspects /conformance to enable recursive browsing and item loading
STAC_CONFORMANCE = [
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/collections",
    "https://api.stacspec.org/v1.0.0/ogcapi-features",
    "https://api.stacspec.org/v1.0.0/item-search",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
]

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


# ============================================================================
# Search Schema
# ============================================================================

class STACSearchRequest(BaseModel):
    collections: Optional[list[str]] = None
    bbox: Optional[list[float]] = None
    datetime: Optional[str] = None
    ids: Optional[list[str]] = None
    limit: int = 100


# ============================================================================
# URL Helpers
# ============================================================================

def _stac_base_url(request: Request) -> str:
    """Return absolute URL of STAC landing page."""
    return str(request.url_for("get_stac_catalog")).rstrip("/")


def _wfs_url(request: Request, layer_uuid: str) -> str:
    """Return absolute DAMAPQL GeoJSON endpoint URL for a DB layer."""
    try:
        return str(request.url_for("get_wfs", uuid=layer_uuid, format="geojson"))
    except Exception:
        root = str(request.base_url).rstrip("/")
        return f"{root}/api/wfs/{layer_uuid}/geojson/"


# ============================================================================
# Layer Helpers
# ============================================================================

def _layer_collection_id(info: LayerInfo) -> str:
    if info.layer_category:
        return info.layer_category.slug
    return info.app_label or "default"


def _layer_datetime_value(info: LayerInfo) -> Optional[datetime]:
    value = info.temporal_res or info.uploaded_date
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return None


def _layer_datetime(info: LayerInfo) -> Optional[str]:
    dt = _layer_datetime_value(info)
    if dt is None:
        return None
    value = dt.isoformat()
    if dt.tzinfo is None:
        value += "Z"
    return value


def _layer_extent_4326(info: LayerInfo) -> list[float]:
    if not info.extent or len(info.extent) != 4:
        raise ValueError(f"Layer '{info.uuid}' does not have a valid extent.")

    minx, miny, maxx, maxy = [float(v) for v in info.extent]
    srid = info.srid or 4326

    if srid == 4326:
        return [minx, miny, maxx, maxy]

    transformer = Transformer.from_crs(f"EPSG:{srid}", "EPSG:4326", always_xy=True)
    x1, y1 = transformer.transform(minx, miny)
    x2, y2 = transformer.transform(maxx, maxy)

    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


# ============================================================================
# Asset Helpers (Roles updated for QGIS display)
# ============================================================================
#
# def _get_media_type(info: LayerInfo) -> Optional[str]:
#     fmt = (info.format or "").strip().upper()
#     if fmt == "COG":
#         return COG_MEDIA_TYPE
#     if fmt in {"TIF", "TIFF", "GEOTIFF"}:
#         return "image/tiff; application=geotiff"
#     if fmt == "GEOJSON":
#         return "application/geo+json"
#     if fmt == "MVT":
#         return "application/vnd.mapbox-vector-tile"
#     if fmt == "JSON":
#         return "application/json"
#     return None

def _get_media_type(info: LayerInfo) -> str:
    fmt = (info.format or "").strip().upper()

    if fmt in {"COG", "TIF", "TIFF", "GEOTIFF"}:
        # Must be clean for QGIS raster data provider
        return "image/tiff; application=geotiff"

    if fmt == "GEOJSON":
        return "application/geo+json"

    return "image/tiff"


from typing import Any
from urllib.parse import unquote
from fastapi import Request

def _layer_asset(
    info: LayerInfo,
    request: Request,
    minio_manager: MinioManager | None = None,
) -> dict[str, Any]:
    """
    Resolve STAC assets for LayerInfo.

    Handles:
      - Rasters (MinIO/S3/File): Exposes direct COG streaming/download and
        the server-side TMS tile service (/raster_tile/{uuid}/{z}/{x}/{y}).
      - Vectors (DB): Uses info.data_url if available; otherwise falls back to
        the dynamic Mapbox Vector Tile service (/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}).
      - Direct URLs: Handles external static files or API endpoints.
    """
    access = info.layer_accessibility
    if not access:
        return {}

    root = str(request.base_url).rstrip("/")
    uuid_str = str(info.uuid)
    layer_title = info.title or info.layer_name or "Layer"
    assets: dict[str, Any] = {}

    # ========================================================================
    # 1. RASTER ASSETS (MinIO, S3, File)
    # ========================================================================
    if access.is_minio or access.is_s3 or access.is_file:
        cog_href: str | None = None

        if access.is_minio and minio_manager:
            bucket = access.s3_bucket
            object_name = access.s3_object_name
            if bucket and object_name:
                cog_href = minio_manager.generate_presigned_url(
                    bucket_name=bucket,
                    object_name=object_name,
                    expires_minutes=120,
                )
        elif access.is_s3:
            cog_href = access.s3_uri
        elif access.is_file and access.file_path:
            path = access.file_path
            cog_href = path.as_uri() if hasattr(path, "as_uri") else str(path)

        if cog_href:
            assets["cog"] = {
                "href": cog_href,
                "title": f"{layer_title} (Cloud-Optimized GeoTIFF)",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
            }

        # Direct TMS Raster Tile Service
        try:
            raw_url = str(
                request.url_for("get_layer_tms", uuid=uuid_str, z="{z}", x="{x}", y="{y}")
            )
            tile_url = unquote(raw_url)
            da_logger.debug(f"TMS URL: {tile_url}")
        except Exception:
            tile_url = f"{root}/api/dch/raster_tile/{uuid_str}/{{z}}/{{x}}/{{y}}"

        assets["tiles"] = {
            "href": tile_url,
            "title": f"{layer_title} (TMS Raster Tiles)",
            "type": "image/png",
            "roles": ["visual", "tiles"],
        }
        assets["visual"] = dict(assets["tiles"])
        return assets

    # ========================================================================
    # 2. VECTOR / DATABASE ASSETS
    # ========================================================================
    if access.is_db:
        if info.data_url and str(info.data_url).strip():
            raw_url = str(info.data_url).strip()
            raw_url = raw_url.replace("{uuid}", uuid_str).replace("%7Buuid%7D", uuid_str)

            href = f"{root}{raw_url}" if raw_url.startswith("/") else raw_url
            fmt = (info.format or "").strip().upper()
            media_type = (
                "application/vnd.mapbox-vector-tile"
                if fmt == "MVT" or "mvt" in href.lower()
                else "application/geo+json"
            )

            assets["data"] = {
                "href": href,
                "title": layer_title,
                "type": media_type,
                "roles": ["data", "visual"],
            }
            return assets

        # Fallback to dynamic MVT generation
        tile_size = 512
        try:
            raw_mvt = str(
                request.url_for(
                    "get_layer_mvt",
                    uuid=uuid_str,
                    tile_size=tile_size,
                    z="{z}",
                    x="{x}",
                    y="{y}",
                )
            )
            mvt_url = unquote(raw_mvt)
            da_logger.debug(f"MVT URL: {mvt_url}")
        except Exception:
            mvt_url = f"{root}/api/dch/layer_mvt/{uuid_str}/{tile_size}/{{z}}/{{x}}/{{y}}"

        assets["tiles"] = {
            "href": mvt_url,
            "title": f"{layer_title} (Mapbox Vector Tiles)",
            "type": "application/vnd.mapbox-vector-tile",
            "roles": ["data", "visual", "tiles"],
        }
        assets["data"] = dict(assets["tiles"])
        return assets

    # ========================================================================
    # 3. DIRECT URL FALLBACK
    # ========================================================================
    if access.is_url:
        assets["data"] = {
            "href": access.url or "",
            "title": layer_title,
            "type": _get_media_type(info) if "_get_media_type" in globals() else "application/json",
            "roles": ["data", "visual"],
        }
        return assets

    return {}



# ============================================================================
# STAC Item Serializer
# ============================================================================

def _to_stac_item(
    info: LayerInfo, request: Request, minio_manager: MinioManager
) -> dict[str, Any]:
    bbox = _layer_extent_4326(info)
    minx, miny, maxx, maxy = bbox

    geometry = {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny],
            [maxx, miny],
            [maxx, maxy],
            [minx, maxy],
            [minx, miny],
        ]],
    }

    collection_id = _layer_collection_id(info)
    base_url = _stac_base_url(request)

    item_url = f"{base_url}/collections/{collection_id}/items/{info.uuid}"
    collection_url = f"{base_url}/collections/{collection_id}"

    properties: dict[str, Any] = {
        "datetime": _layer_datetime(info),
        "title": info.title or info.layer_name,
        # Required by QGIS to instantiate the raster provider
        "proj:epsg": int(info.srid or 4326),
        "damapql:layer_name": info.layer_name,
        "damapql:format": info.format,
        "damapql:srid": info.srid,
        "damapql:data_model": info.data_model.code if info.data_model else None,
        "damapql:model_type": info.model_type.code if info.model_type else None,
    }
    properties = {k: v for k, v in properties.items() if v is not None}

    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v1.0.0/schema.json"
        ],
        "id": str(info.uuid),
        "collection": collection_id,
        "bbox": [minx, miny, maxx, maxy],
        "geometry": geometry,
        "properties": properties,
        "assets": _layer_asset(
            info=info, request=request, minio_manager=minio_manager
        ),
        "links": [
            {"rel": "self", "href": item_url, "type": "application/geo+json"},
            {
                "rel": "collection",
                "href": collection_url,
                "type": "application/json",
            },
            {"rel": "parent", "href": collection_url, "type": "application/json"},
            {"rel": "root", "href": base_url, "type": "application/json"},
        ],
    }

# ============================================================================
# Database Helpers
# ============================================================================

def _get_all_layers() -> list[LayerInfo]:
    with LayerInfo.managed_session() as session:
        stmt = (
            select(LayerInfo)
            .options(
                selectinload(LayerInfo.layer_accessibility),
                selectinload(LayerInfo.layer_category),
                selectinload(LayerInfo.quality_level),
            )
            .order_by(LayerInfo.uploaded_date.desc())
        )
        return list(session.execute(stmt).scalars().all())


def _get_collection_layers(collection_id: str) -> list[LayerInfo]:
    return [layer for layer in _get_all_layers() if _layer_collection_id(layer) == collection_id]


# ============================================================================
# Collection Serializer
# ============================================================================

def _build_collection(*, collection_id: str, layers: list[LayerInfo], request: Request) -> dict[str, Any]:
    if not layers:
        raise ValueError(f"No layers found for collection '{collection_id}'.")

    base_url = _stac_base_url(request)

    # 1. Spatial extent
    valid_bboxes: list[list[float]] = []
    for layer in layers:
        try:
            valid_bboxes.append(_layer_extent_4326(layer))
        except Exception:
            continue

    if valid_bboxes:
        spatial_bbox = [
            min(b[0] for b in valid_bboxes),
            min(b[1] for b in valid_bboxes),
            max(b[2] for b in valid_bboxes),
            max(b[3] for b in valid_bboxes),
        ]
    else:
        spatial_bbox = [-180.0, -90.0, 180.0, 90.0]

    # 2. Temporal extent
    datetimes = [dt for dt in (_layer_datetime_value(layer) for layer in layers) if dt is not None]
    start_datetime = min(datetimes).isoformat() if datetimes else None
    end_datetime = max(datetimes).isoformat() if datetimes else None

    first_layer = layers[0]
    title = str(first_layer.layer_category) if first_layer.layer_category else collection_id
    collection_url = f"{base_url}/collections/{collection_id}"

    # Links include items rel so QGIS can enumerate features
    links = [
        {"rel": "self", "href": collection_url, "type": "application/json"},
        {"rel": "items", "href": f"{collection_url}/items", "type": "application/geo+json"},
        {"rel": "root", "href": base_url, "type": "application/json"},
        {"rel": "parent", "href": f"{base_url}/collections", "type": "application/json"},
    ]

    # Add child item links to support older QGIS / STAC crawlers
    for layer in layers:
        links.append({
            "rel": "item",
            "href": f"{collection_url}/items/{layer.uuid}",
            "type": "application/geo+json",
        })

    return {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "id": collection_id,
        "title": title,
        "description": f"DAMAPQL spatial data collection: {title}",
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [spatial_bbox]},
            "temporal": {"interval": [[start_datetime, end_datetime]]},
        },
        "links": links,
    }


# ============================================================================
# Search Helpers
# ============================================================================

def _parse_bbox(bbox: Optional[str]) -> Optional[list[float]]:
    if not bbox:
        return None
    try:
        values = [float(v.strip()) for v in bbox.split(",")]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid bbox format.")

    if len(values) != 4:
        raise HTTPException(status_code=400, detail="bbox must contain exactly four values: minx,miny,maxx,maxy")

    return values


def _bbox_intersects(first: list[float], second: list[float]) -> bool:
    return not (
        first[2] < second[0] or
        first[0] > second[2] or
        first[3] < second[1] or
        first[1] > second[3]
    )


def _parse_search_datetime(value: Optional[str]) -> tuple[Optional[datetime], Optional[datetime]]:
    if not value:
        return None, None

    def parse_dt(text: str) -> Optional[datetime]:
        if not text or text == "..":
            return None
        text = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text)
                return datetime.combine(parsed_date, time.min)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid datetime: {text}")

    if "/" in value:
        start_text, end_text = value.split("/", 1)
        return parse_dt(start_text), parse_dt(end_text)

    dt = parse_dt(value)
    return dt, dt


def _datetime_matches(info: LayerInfo, start_datetime: Optional[datetime], end_datetime: Optional[datetime]) -> bool:
    if start_datetime is None and end_datetime is None:
        return True

    layer_dt = _layer_datetime_value(info)
    if layer_dt is None:
        return False

    if layer_dt.tzinfo is not None and start_datetime and start_datetime.tzinfo is None:
        start_datetime = start_datetime.replace(tzinfo=layer_dt.tzinfo)

    if layer_dt.tzinfo is not None and end_datetime and end_datetime.tzinfo is None:
        end_datetime = end_datetime.replace(tzinfo=layer_dt.tzinfo)

    if start_datetime and layer_dt < start_datetime:
        return False

    if end_datetime and layer_dt > end_datetime:
        return False

    return True


def _search_layers(
    *,
    collections: Optional[list[str]] = None,
    bbox: Optional[list[float]] = None,
    datetime_value: Optional[str] = None,
    ids: Optional[list[str]] = None,
    limit: int = 100,
) -> list[LayerInfo]:
    layers = _get_all_layers()
    requested_collections = set(collections) if collections else None
    requested_ids = set(ids) if ids else None
    start_datetime, end_datetime = _parse_search_datetime(datetime_value)

    result: list[LayerInfo] = []

    for info in layers:
        if requested_collections and _layer_collection_id(info) not in requested_collections:
            continue

        if requested_ids and str(info.uuid) not in requested_ids:
            continue

        if bbox:
            try:
                layer_bbox = _layer_extent_4326(info)
            except Exception:
                continue
            if not _bbox_intersects(layer_bbox, bbox):
                continue

        if not _datetime_matches(info, start_datetime, end_datetime):
            continue

        result.append(info)
        if len(result) >= limit:
            break

    return result


# ============================================================================
# STAC Landing Page & Conformance
# ============================================================================

@stac_router.get("", name="get_stac_catalog", summary="STAC Catalog")
@stac_router.get("/", include_in_schema=False)
def get_stac_catalog(request: Request):
    base_url = _stac_base_url(request)

    return {
        "type": "Catalog",
        "id": "damapql",
        "title": "DAMAPQL Spatial Data Catalog",
        "description": "SpatioTemporal Asset Catalog for DAMAPQL spatial datasets.",
        "stac_version": STAC_VERSION,
        "stac_extensions": [],
        "conformsTo": STAC_CONFORMANCE,
        "links": [
            {"rel": "self", "href": base_url, "type": "application/json"},
            {"rel": "root", "href": base_url, "type": "application/json"},
            {"rel": "data", "href": f"{base_url}/collections", "type": "application/json"},
            {"rel": "conformance", "href": f"{base_url}/conformance", "type": "application/json"},
            {"rel": "search", "href": f"{base_url}/search", "type": "application/geo+json", "method": "GET"},
            {"rel": "search", "href": f"{base_url}/search", "type": "application/geo+json", "method": "POST"},
        ],
    }


@stac_router.get("/conformance", summary="STAC Conformance")
def get_stac_conformance():
    return {"conformsTo": STAC_CONFORMANCE}


# ============================================================================
# Collections
# ============================================================================

@stac_router.get("/collections", summary="Get STAC Collections")
def get_stac_collections(request: Request):
    base_url = _stac_base_url(request)
    layers = _get_all_layers()

    grouped: dict[str, list[LayerInfo]] = {}
    for layer in layers:
        collection_id = _layer_collection_id(layer)
        grouped.setdefault(collection_id, []).append(layer)

    collections = [
        _build_collection(collection_id=cid, layers=c_layers, request=request)
        for cid, c_layers in grouped.items()
    ]

    return {
        "collections": collections,
        "links": [
            {"rel": "self", "href": f"{base_url}/collections", "type": "application/json"},
            {"rel": "root", "href": base_url, "type": "application/json"},
        ],
    }


@stac_router.get("/collections/{collection_id}", summary="Get STAC Collection")
def get_stac_collection(collection_id: str, request: Request):
    layers = _get_collection_layers(collection_id)
    if not layers:
        raise HTTPException(status_code=404, detail=f"STAC collection '{collection_id}' not found.")

    return _build_collection(collection_id=collection_id, layers=layers, request=request)


# ============================================================================
# Collection Items (OGC API Features / STAC)
# ============================================================================

@stac_router.get("/collections/{collection_id}/items", summary="Get STAC Collection Items")
def get_stac_collection_items(
    collection_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    bbox: Optional[str] = Query(None, description="EPSG:4326 bbox: minx,miny,maxx,maxy"),
    datetime_value: Optional[str] = Query(None, alias="datetime"),
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    parsed_bbox = _parse_bbox(bbox)
    layers = _search_layers(
        collections=[collection_id],
        bbox=parsed_bbox,
        datetime_value=datetime_value,
        limit=limit,
    )

    features = []
    for layer in layers:
        try:
            features.append(_to_stac_item(info=layer, request=request, minio_manager=minio_manager))
        except Exception:
            continue

    base_url = _stac_base_url(request)
    return {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "links": [
            {"rel": "self", "href": f"{base_url}/collections/{collection_id}/items", "type": "application/geo+json"},
            {"rel": "collection", "href": f"{base_url}/collections/{collection_id}", "type": "application/json"},
            {"rel": "root", "href": base_url, "type": "application/json"},
        ],
    }


@stac_router.get("/collections/{collection_id}/items/{item_id}", summary="Get STAC Item")
def get_stac_item(
    collection_id: str,
    item_id: str,
    request: Request,
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    info = LayerInfo.get_layer_info_by_uuid(item_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"STAC item '{item_id}' not found.")

    if _layer_collection_id(info) != collection_id:
        raise HTTPException(status_code=404, detail="Item does not belong to this collection.")

    return _to_stac_item(info=info, request=request, minio_manager=minio_manager)


# ============================================================================
# Search Endpoints
# ============================================================================

@stac_router.get("/search", summary="Search STAC Items")
def search_stac_get(
    request: Request,
    collections: Optional[str] = Query(None, description="Comma-separated collection IDs."),
    ids: Optional[str] = Query(None, description="Comma-separated STAC item IDs."),
    bbox: Optional[str] = Query(None, description="EPSG:4326 bbox: minx,miny,maxx,maxy"),
    datetime_value: Optional[str] = Query(None, alias="datetime"),
    limit: int = Query(100, ge=1, le=1000),
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    parsed_collections = [v.strip() for v in collections.split(",") if v.strip()] if collections else None
    parsed_ids = [v.strip() for v in ids.split(",") if v.strip()] if ids else None
    parsed_bbox = _parse_bbox(bbox)

    layers = _search_layers(
        collections=parsed_collections,
        bbox=parsed_bbox,
        datetime_value=datetime_value,
        ids=parsed_ids,
        limit=limit,
    )

    features = []
    for layer in layers:
        try:
            features.append(_to_stac_item(info=layer, request=request, minio_manager=minio_manager))
        except Exception:
            continue

    return {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "links": [
            {"rel": "self", "href": str(request.url), "type": "application/geo+json"},
            {"rel": "root", "href": _stac_base_url(request), "type": "application/json"},
        ],
    }


@stac_router.post("/search", summary="Search STAC Items")
def search_stac_post(
    payload: STACSearchRequest,
    request: Request,
    minio_manager: MinioManager = Depends(get_minio_manager),
):
    if payload.bbox and len(payload.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox must contain exactly four values.")

    limit = max(1, min(payload.limit, 1000))
    layers = _search_layers(
        collections=payload.collections,
        bbox=payload.bbox,
        datetime_value=payload.datetime,
        ids=payload.ids,
        limit=limit,
    )

    features = []
    for layer in layers:
        try:
            features.append(_to_stac_item(info=layer, request=request, minio_manager=minio_manager))
        except Exception:
            continue

    return {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features),
        "links": [
            {"rel": "root", "href": _stac_base_url(request), "type": "application/json"},
        ],
    }