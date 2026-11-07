from typing import List, Optional, Union, Any

import jenkspy
import pandas as pd
from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select, text, inspect, func
from sqlalchemy.orm import Session, selectinload

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.config_registry import UserAOIService, BoundaryHierarchyService
from app.damapql.dch.dtypes import AttributeTableResponse
from app.damapql.dch.models import LayerInfo, DataModel, DBQuery, LayerCategory
from app.damapql.dch.schemas.geojson import GeoJSON
from app.damapql.dch.services.raster_stats_service import RasterStatsService
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from app.damapql.utils.adapters.deps import get_minio_manager
from digitalarzengine.io.managers.minio_manager import MinioManager

from app.damapql.utils.adapters.model_utils import ModelUtils

dch_layers_router = APIRouter(tags=["dch"])


def handle_timestamp(obj):
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


@dch_layers_router.get("/layer_categories/")
def get_layer_categories(current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    with LayerCategory.managed_session() as session:
        categories = session.execute(
            select(LayerCategory.id, LayerCategory.main, LayerCategory.sub).order_by(
                LayerCategory.main, LayerCategory.sub
            )
        ).all()

        return {
            "payload": [
                {
                    "pk": category.id,
                    "main": category.main,
                    "sub": category.sub,
                    "name": (f"{category.main} — {category.sub}" if category.sub else category.main),
                }
                for category in categories
            ]
        }


@dch_layers_router.get("/wfs/{uuid}/{format}/")
async def get_wfs(
    uuid: str,
    format: str,
    limit: int = Query(20000, ge=1, le=200000),
    offset: int = Query(0, ge=0),
    cols: Optional[List[str]] = Query(None),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns vector layer features as GeoJSON.
    """
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        return Response(status_code=204)

    access = info.layer_accessibility
    engine = access.get_external_engine() if access else None
    if not engine:
        return Response(status_code=204)

    fmt = (format or "").lower()
    if fmt not in ("geojson", "json"):
        raise HTTPException(status_code=400, detail="Only GeoJSON is supported for now")

    if not info.srid:
        raise HTTPException(status_code=400, detail="Layer SRID missing")

    if not access.geometry_col:
        raise HTTPException(status_code=400, detail="Geometry column missing")

    table = DBQuery.safe_ident(access.table_name)
    geom_col = DBQuery.safe_ident(access.geometry_col)

    allowed = set(access.cols_name or [])
    requested = cols or list(allowed)

    bad = [c for c in requested if c not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

    safe_cols = ", ".join(DBQuery.safe_ident(c) for c in requested)
    select_list = f"{safe_cols}, {geom_col}" if safe_cols else geom_col

    pk_cols = access.pk_cols or []
    order_by = ""
    if pk_cols:
        safe_pks = ", ".join(DBQuery.safe_ident(c) for c in pk_cols)
        order_by = f" ORDER BY {safe_pks}"

    sql = f"SELECT {select_list} FROM {table}{order_by} LIMIT :limit OFFSET :offset"

    with Session(engine) as session:
        gdf = DBQuery.execute_query_as_gdf(
            session,
            sql,
            params={"limit": limit, "offset": offset},
            geom_col=access.geometry_col,
            crs=f"EPSG:{info.srid}",
        )

    if gdf.empty:
        return Response(status_code=204)

    gdf_4326 = gdf.to_crs(epsg=4326)
    geojson_obj = GeoJSON.from_gdf(gdf_4326)

    return {
        "payload": DCHUtils.gzip_data(geojson_obj.model_dump_json(by_alias=True)),
        "meta": {"format": "geojson", "limit": limit, "offset": offset, "count": len(gdf_4326)},
    }


@dch_layers_router.get("/layer_fields/{uuid}/")
async def get_layer_fields(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns available column names for a layer.
    """
    with LayerInfo.managed_session() as session:
        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

        if obj is not None and obj.layer_accessibility:
            params = obj.layer_accessibility.params or {}
            columns = params.get("columns", [])
            return {"payload": columns}

    return Response(status_code=204)


@dch_layers_router.get("/layer_field_distinct_values/{uuid}/{field_name}/{field_type}/")
async def get_field_distinct_values(
    uuid: str,
    field_name: str,
    field_type: str,
    classification: str = "Stats",
    no_of_classes: int = 0,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns distinct field values or classification breaks.
    """
    with LayerInfo.managed_session() as session:
        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

        if obj is None:
            return Response(status_code=204)

        df = obj.to_layer_df(cols=[field_name])

    if df.empty or field_name not in df.columns:
        return {"payload": []}

    res = []

    if field_type == "number" and no_of_classes > 0:
        data_series = df[field_name].dropna()
        if data_series.empty:
            return {"payload": []}

        if classification.lower() == "quantile":
            bins = pd.qcut(data_series, q=int(no_of_classes), duplicates="drop")
            res = sorted([float(interval.right) for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "EqualInterval":
            bins = pd.cut(data_series, bins=int(no_of_classes))
            res = sorted([float(interval.right) for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "NaturalBreak":
            res = [float(x) for x in jenkspy.jenks_breaks(data_series.tolist(), n_classes=int(no_of_classes))]

        else:
            res = [x.item() if hasattr(x, "item") else x for x in data_series.unique()]
    else:
        res = [x.item() if hasattr(x, "item") else x for x in df[field_name].dropna().unique()]

    return {"payload": res}


def parse_extent_3857(extent_3857: str | None) -> list[float] | None:
    if not extent_3857:
        return None

    try:
        values = [float(v) for v in extent_3857.split(",")]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid extent_3857")

    if len(values) != 4:
        raise HTTPException(status_code=400, detail="extent_3857 must have 4 values")

    return values


def _build_spatial_sql_filter(
    quoted_geom: str,
    target_srid: int,
    layer_name: Optional[str] = None,
    spatial_id: Optional[str | int] = None,
    available_cols: Optional[set[str]] = None,
    boundary_geom: Optional[str] = None,
    **kwargs,
) -> tuple[Optional[str], dict[str, Any]]:
    """
    Builds the dynamic navigation filter clause.
    1. First checks for explicit relational foreign keys on the layer table.
    2. Falls back to PostGIS spatial intersection using the boundary geometry
       retrieved via BoundaryHierarchyService.
    """
    if not layer_name or spatial_id is None:
        return None, {}

    available_cols = available_cols or set()
    clean_layer = str(layer_name).lower().replace(" ", "").replace("_", "")

    # 1. Relational Fast-Path
    candidate_cols = {
        f"{clean_layer}_id",
        f"{clean_layer}id",
        f"id_{clean_layer}",
        f"{str(layer_name).lower()}_id",
    }

    matched_col = None
    for col in available_cols:
        col_clean = col.lower().replace(" ", "").replace("_", "")
        if col.lower() in candidate_cols or col_clean in candidate_cols:
            matched_col = col
            break

    if matched_col:
        return f"{DBQuery.safe_ident(matched_col)} = :nav_spatial_id", {"nav_spatial_id": spatial_id}

    # 2. Spatial Fallback via BoundaryHierarchyService
    boundary_wkt = BoundaryHierarchyService.get_boundary_wkt(layer_name, spatial_id)
    if boundary_wkt:
        boundary_srid = 3857
        clause = (
            f"ST_Intersects({quoted_geom}, "
            f"ST_Transform(ST_GeomFromText(:nav_boundary_wkt, {boundary_srid}), ST_SRID({quoted_geom})))"
        )
        return clause, {"nav_boundary_wkt": boundary_wkt}

    return None, {}


def _apply_layer_scope_clauses(
    quoted_geom: str,
    target_srid: int,
    current_user: UserInfo,
    layer_name: Optional[str] = None,
    spatial_id: Optional[str | int] = None,
    available_cols: Optional[set[str]] = None,
    extent_3857: Optional[str] = None,
) -> tuple[list[str], dict[str, Any]]:
    """
    Constructs WHERE clauses and query parameters:
    - Extent filter is always applied if provided.
    - If a navigation boundary filter is active (layer_name & spatial_id), it takes PRECEDENCE.
    - If no boundary filter is active, falls back to the user's base AOI boundary.
    """
    where_clauses: list[str] = [f"{quoted_geom} IS NOT NULL"]
    params: dict[str, Any] = {}

    # 1. Viewport Extent Filter (EPSG:3857)
    extent_filter = parse_extent_3857(extent_3857)
    if extent_filter:
        minx, miny, maxx, maxy = extent_filter
        where_clauses.append(
            f"ST_Intersects({quoted_geom}, "
            f"ST_Transform(ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 3857), {target_srid}))"
        )
        params.update({"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy})

    # 2. Prefer Navigation Boundary Filter over User AOI
    has_boundary_filter = bool(layer_name and spatial_id is not None)
    if has_boundary_filter:
        spatial_clause, spatial_params = _build_spatial_sql_filter(
            quoted_geom=quoted_geom,
            target_srid=target_srid,
            layer_name=layer_name,
            spatial_id=spatial_id,
            available_cols=available_cols,
        )
        if spatial_clause:
            where_clauses.append(spatial_clause)
            params.update(spatial_params)
    else:
        # Fallback to User AOI boundary when no navigation filter is applied
        if not current_user.is_superuser:
            aoi_wkt = UserAOIService.get_wkt(current_user)
            if aoi_wkt:
                aoi_srid = 3857
                where_clauses.append(
                    f"ST_Intersects({quoted_geom}, "
                    f"ST_Transform(ST_GeomFromText(:aoi_wkt, {aoi_srid}), {target_srid}))"
                )
                params["aoi_wkt"] = aoi_wkt

    return where_clauses, params


@dch_layers_router.get(
    "/layer_attributes/{uuid}/",
    response_model=Union[AttributeTableResponse, dict],
    summary="Get Layer Attributes",
)
def get_layer_attributes(
    uuid: str,
    extent_3857: Optional[str] = Query(None),
    layer_name: Optional[str] = Query(None),
    spatial_id: Optional[str] = Query(None),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
    minio_manager: MinioManager | None = Depends(get_minio_manager),
):
    """
    Returns attribute table for vector layers (preferring boundary filter over User AOI)
    or summary data for raster layers.
    """
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(
                selectinload(LayerInfo.layer_accessibility),
                selectinload(LayerInfo.layer_category),
                selectinload(LayerInfo.quality_level),
            )
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if info is None:
        return Response(status_code=204)

    # VECTOR LAYER
    if info.data_model and info.data_model.code == DataModel.V.name:
        access = info.layer_accessibility
        if not access:
            return Response(status_code=204)

        geom_col_name = access.geometry_col or "geom"
        quoted_geom = DBQuery.safe_ident(geom_col_name)
        target_srid = info.srid or 4326
        available_cols = {c.lower() for c in (access.cols_name or [])}

        where_clauses, params = _apply_layer_scope_clauses(
            quoted_geom=quoted_geom,
            target_srid=target_srid,
            current_user=current_user,
            layer_name=layer_name,
            spatial_id=spatial_id,
            available_cols=available_cols,
            extent_3857=extent_3857,
        )

        combined_where = " AND ".join(where_clauses) if where_clauses else None

        if extent_3857:
            gdf = info.to_layer_gdf(where_clause=combined_where)
            if gdf.empty:
                return Response(status_code=204)
            df = pd.DataFrame(gdf.drop(columns=[geom_col_name], errors="ignore"))
        else:
            df = info.to_layer_df(where_clause=combined_where)

        if not df.empty:
            return {
                "payload": ManageDataFrameUtils.grid_response(
                    df, pkCols=access.pk_cols
                )
            }

        return Response(status_code=204)

    # RASTER LAYER
    elif info.data_model and info.data_model.code == DataModel.R.name:
        return {
            "payload": RasterStatsService.get_attribute_summary_grid(
                uuid=uuid, minio_manager=minio_manager
            )
        }

    return Response(status_code=204)


@dch_layers_router.get("/get_feature_detail/{uuid}/{col_name}/{col_val}/")
async def get_feature_detail(
    uuid: str,
    col_name: str,
    col_val: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

    if info is None:
        return Response(status_code=204)

    rs = info.get_row_detail(col_name, col_val)

    if rs and len(rs) > 0:
        row_dict = dict(rs[0])
        row_dict.pop("geom", None)
        row_dict.pop("geometry", None)
        return {"payload": row_dict}

    return Response(status_code=204)


@dch_layers_router.get("/get_feature_geometry/{uuid}/{pk_values}/")
async def get_feature_geometry(
    uuid: str,
    pk_values: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return Response(status_code=204)

    access = info.layer_accessibility
    pk_cols = access.pk_cols or []
    vals = pk_values.split(",")

    if len(pk_cols) != len(vals):
        return {"payload": {"msg": "PK column and value count mismatch"}}

    params = {}
    clauses = []

    for i, col in enumerate(pk_cols):
        pname = f"v{i}"
        clauses.append(f"{DBQuery.safe_ident(col)} = :{pname}")
        params[pname] = vals[i]

    where_sql = " AND ".join(clauses)

    engine = access.get_external_engine()
    if not engine:
        return Response(status_code=204)

    geom_col = DBQuery.safe_ident(access.geometry_col)
    table = DBQuery.safe_ident(access.table_name)

    q = text(
        f"SELECT ST_AsText(ST_Transform({geom_col}, 3857)) AS geom "
        f"FROM {table} WHERE {where_sql} LIMIT 1"
    )

    with Session(engine) as session:
        geom = session.execute(q, params).scalar_one_or_none()

    return {"payload": geom} if geom else Response(status_code=204)


@dch_layers_router.get("/column_value/{uuid}/{pk_val}/{col_name}/")
async def get_column_value(
    uuid: str,
    pk_val: str,
    col_name: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return {"payload": {col_name: ""}}

    access = info.layer_accessibility
    allowed_cols = set(access.cols_name or [])
    if col_name not in allowed_cols:
        raise HTTPException(status_code=400, detail="Invalid column")

    pk_cols = access.pk_cols or []
    pk_vals = pk_val.split(info.pk_concat_char or "-")

    if len(pk_cols) == 0:
        return {"payload": {col_name: ""}}

    params = {}
    clauses = []
    for i, col in enumerate(pk_cols):
        if i >= len(pk_vals):
            break
        pname = f"p{i}"
        clauses.append(f"{DBQuery.safe_ident(col)} = :{pname}")
        params[pname] = pk_vals[i]

    where_sql = " AND ".join(clauses)
    if not where_sql:
        return {"payload": {col_name: ""}}

    engine = access.get_external_engine()
    if not engine:
        return {"payload": {col_name: ""}}

    table = DBQuery.safe_ident(access.table_name)
    target_col = DBQuery.safe_ident(col_name)

    q = text(f"SELECT {target_col} FROM {table} WHERE {where_sql} LIMIT 1")
    with Session(engine) as session:
        val = session.execute(q, params).scalar_one_or_none()

    return {"payload": {col_name: val if val is not None else ""}}


class SpatialSelectRequest(BaseModel):
    geometry: str
    srid: int = 3857
    inverse: bool = False


@dch_layers_router.post("/spatial_select/{uuid}/")
async def spatial_selection(
    uuid: str,
    data: SpatialSelectRequest,
):
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        return Response(status_code=204)

    g_col = info.layer_accessibility.geometry_col
    safe_g_col = DBQuery.safe_ident(g_col)
    target_srid = info.srid or 4326

    geom_sql = f"ST_Transform(ST_GeomFromText('{data.geometry}', {data.srid}), {target_srid})"

    if data.inverse:
        spatial_where = f"NOT ST_Intersects({safe_g_col}, {geom_sql})"
    else:
        spatial_where = f"ST_Intersects({safe_g_col}, {geom_sql})"

    gdf = info.to_layer_gdf(where_clause=spatial_where)

    if gdf.empty:
        return Response(status_code=204)

    return {
        "payload": gdf.to_crs(epsg=4326).__geo_interface__,
        "meta": {"count": len(gdf), "layer_uuid": uuid},
    }


@dch_layers_router.get("/layer_pk_cols/{uuid}/")
async def get_layer_pk_cols(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return Response(status_code=204)

    pk_cols = info.layer_accessibility.pk_cols or []
    return {"pkCols": pk_cols, "meta": {"uuid": uuid, "count": len(pk_cols)}}


class LayerAttributesMetaResponse(BaseModel):
    columns: List[dict]
    total_count: int
    pkCols: List[str]


@dch_layers_router.get(
    "/layer_attributes_meta/{uuid}/",
    summary="Get Layer Attribute Schema and Count (Vector & Raster)",
)
def get_layer_attributes_meta(
    uuid: str,
    extent_3857: Optional[str] = Query(None),
    layer_name: Optional[str] = Query(None),
    spatial_id: Optional[str] = Query(None),
    boundary_geom: Optional[str] = Query(None),
    minio_manager: MinioManager = Depends(get_minio_manager),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info: LayerInfo = (
            session.execute(
                select(LayerInfo)
                .options(selectinload(LayerInfo.layer_accessibility))
                .where(LayerInfo.uuid == uuid)
            )
            .scalar_one_or_none()
        )

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found or has no accessible data source")

    if info.data_model and info.data_model.code in (DataModel.R.name, DataModel.R.value, "Raster"):
        try:
            return RasterStatsService.get_attribute_meta(uuid=uuid, minio_manager=minio_manager)
        except Exception as e:
            da_logger.error(f"Error generating raster attribute summary for {uuid}: {e}")
            return {"columns": [], "total_count": 0, "pkCols": []}

    access = info.layer_accessibility
    engine = access.get_external_engine()
    if not engine or not access.table_name:
        raise HTTPException(status_code=400, detail="External database engine or table name not configured")

    table_parts = access.table_name.split(".")
    quoted_table = ".".join(DBQuery.safe_ident(p) for p in table_parts)
    geom_col_name = access.geometry_col or "geom"
    quoted_geom = DBQuery.safe_ident(geom_col_name)
    target_srid = info.srid or 4326

    available_cols = {c.lower() for c in (access.cols_name or [])}
    where_clauses, params = _apply_layer_scope_clauses(
        quoted_geom=quoted_geom,
        target_srid=target_srid,
        current_user=current_user,
        layer_name=layer_name,
        spatial_id=spatial_id,
        available_cols=available_cols,
        extent_3857=extent_3857,
    )

    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with engine.connect() as conn:
        count_sql = text(f"SELECT COUNT(*) FROM {quoted_table}{where_sql}")
        total_count = conn.execute(count_sql, params).scalar() or 0

        sample_sql = text(f"SELECT * FROM {quoted_table} LIMIT 0")
        sample_df = pd.read_sql(sample_sql, conn)

    known_geom_cols = {geom_col_name.lower(), "geom", "geometry", "the_geom", "shape"}
    cols_to_drop = [c for c in sample_df.columns if c.lower() in known_geom_cols]
    if cols_to_drop:
        sample_df = sample_df.drop(columns=cols_to_drop)

    pk_cols = access.pk_cols or []
    grid_meta = ManageDataFrameUtils.grid_response(sample_df, pkCols=pk_cols)

    return {
        "columns": grid_meta.columns,
        "total_count": total_count,
        "pkCols": pk_cols,
    }


@dch_layers_router.get(
    "/layer_attributes_page/{uuid}/",
    summary="Get Paginated Layer Attribute Records",
)
def get_layer_attributes_page(
    uuid: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    extent_3857: Optional[str] = Query(None),
    layer_name: Optional[str] = Query(None),
    spatial_id: Optional[str] = Query(None),
    boundary_geom: Optional[str] = Query(None),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return Response(status_code=204)

    access = info.layer_accessibility
    engine = access.get_external_engine()
    if not engine or not access.table_name:
        return Response(status_code=204)

    table_parts = access.table_name.split(".")
    quoted_table = ".".join(DBQuery.safe_ident(p) for p in table_parts)
    geom_col_name = access.geometry_col or "geom"
    quoted_geom = DBQuery.safe_ident(geom_col_name)

    allowed_cols = set(access.cols_name or [])
    known_geom_cols = {geom_col_name.lower(), "geom", "geometry", "the_geom", "shape"}
    allowed_cols = {c for c in allowed_cols if c.lower() not in known_geom_cols}

    if allowed_cols:
        cols_sql = ", ".join(DBQuery.safe_ident(c) for c in allowed_cols)
    else:
        with engine.connect() as conn:
            sample_df = pd.read_sql(text(f"SELECT * FROM {quoted_table} LIMIT 0"), conn)
        non_geom_cols = [c for c in sample_df.columns if c.lower() not in known_geom_cols]
        cols_sql = ", ".join(DBQuery.safe_ident(c) for c in non_geom_cols)

    pk_cols = access.pk_cols or []
    order_by = ""
    if pk_cols:
        safe_pks = ", ".join(DBQuery.safe_ident(c) for c in pk_cols)
        order_by = f" ORDER BY {safe_pks}"

    target_srid = info.srid or 4326
    available_cols = {c.lower() for c in (access.cols_name or [])}
    where_clauses, params = _apply_layer_scope_clauses(
        quoted_geom=quoted_geom,
        target_srid=target_srid,
        current_user=current_user,
        layer_name=layer_name,
        spatial_id=spatial_id,
        available_cols=available_cols,
        extent_3857=extent_3857,
    )
    params.update({"limit": limit, "offset": offset})

    where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = text(f"SELECT {cols_sql} FROM {quoted_table}{where_sql}{order_by} LIMIT :limit OFFSET :offset")

    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

    if df.empty:
        return {"payload": []}

    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")

    df = df.round(2)
    cleaned_df = df.astype(object).where(pd.notnull(df), None)

    return {"payload": cleaned_df.to_dict(orient="records")}