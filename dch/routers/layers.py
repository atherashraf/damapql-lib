from typing import List, Optional, Union

import jenkspy
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.dtypes import AttributeTableResponse
from app.damapql.dch.models import LayerInfo, DataModel, DBQuery, LayerCategory
from app.damapql.dch.schemas.geojson import GeoJSON
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils

from app.damapql.dch.services.raster_stats_service import RasterStatsService

dch_layers_router = APIRouter(tags=["dch"])


def handle_timestamp(obj):
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

@dch_layers_router.get("/layer_categories/")
async def layer_categories():
    """
    Returns layer categories.
    Public endpoint.
    """
    with LayerCategory.managed_session() as session:
        stmt = select(LayerCategory)
        categories = session.execute(stmt).scalars().all()

        payload = [
            {
                "pk": obj.id,
                "name": f"{obj.main}-{obj.sub}" if obj.sub else obj.main
            }
            for obj in categories
        ]

    return {"payload": payload}


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
        "meta": {
            "format": "geojson",
            "limit": limit,
            "offset": offset,
            "count": len(gdf_4326),
        },
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
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

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
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if obj is None:
            return Response(status_code=204)

        df = obj.to_layer_df(cols=[field_name]  )

    if df.empty or field_name not in df.columns:
        return {"payload": []}

    res = []

    if field_type == "number" and no_of_classes > 0:
        data_series = df[field_name].dropna()
        if data_series.empty:
            return {"payload": []}

        if classification.lower() == "quantile":
            bins = pd.qcut(data_series, q=int(no_of_classes), duplicates="drop")
            res = sorted([interval.right for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "EqualInterval":
            bins = pd.cut(data_series, bins=int(no_of_classes))
            res = sorted([interval.right for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "NaturalBreak":
            res = jenkspy.jenks_breaks(data_series.tolist(), n_classes=int(no_of_classes))

        else:
            res = data_series.unique().tolist()
    else:
        res = df[field_name].dropna().unique().tolist()

    return {"payload": res}


@dch_layers_router.get(
    "/layer_attributes/{uuid}/",
    response_model=Union[AttributeTableResponse, dict],
    summary="Get Layer Attributes",
)
def get_layer_attributes(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns attribute table for vector layers or summary data for raster layers.
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

    if info.data_model and info.data_model.code == DataModel.V.name:
        df = info.to_layer_df()
        if not df.empty:
            return ManageDataFrameUtils.grid_response(
                df,
                pkCols=info.layer_accessibility.pk_cols
            )

    elif info.data_model and info.data_model.code == DataModel.R.name:
        return RasterStatsService.get_attribute_summary_grid(uuid)

    return Response(content='{"payload": {"msg": "No attribute found"}}', status_code=204)


@dch_layers_router.get("/get_feature_detail/{uuid}/{col_name}/{col_val}/")
async def get_feature_detail(
    uuid: str,
    col_name: str,
    col_val: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns non-geometry details for a single feature.
    """
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

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
    """
    Returns feature geometry as WKT in EPSG:3857 for a given primary key set.
    """
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(
                selectinload(LayerInfo.layer_accessibility)
            )
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
    """
    Returns a single column value for a feature identified by its PK value(s).
    """
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

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
    geometry: str # This matches your frontend key
    srid: int = 3857


@dch_layers_router.post("/spatial_select/{uuid}/")
async def spatial_selection(
        uuid: str,
        data: SpatialSelectRequest,
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        return Response(status_code=204)

    # Get the geometry column name from layer accessibility
    g_col = info.layer_accessibility.geometry_col
    safe_g_col = DBQuery.safe_ident(g_col)

    # Define a single-pass spatial where clause
    # We transform the input WKT (3857) to the layer's native SRID
    spatial_where = f"""
        ST_Intersects(
            {safe_g_col}, 
            ST_Transform(ST_GeomFromText('{data.geometry}', {data.srid}), {info.srid or 4326})
        )
    """

    # execute with the spatial where_clause, skipping extent_3857 to avoid double-filtering
    gdf = info.to_layer_gdf(where_clause=spatial_where)

    if gdf.empty:
        return Response(status_code=204)

    # Return as GeoJSON directly from the database result
    return {
        "payload": gdf.to_crs(epsg=4326).__geo_interface__,
        "meta": {
            "count": len(gdf),
            "layer_uuid": uuid
        }
    }


@dch_layers_router.get("/layer_pk_cols/{uuid}/")
async def get_layer_pk_cols(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns the primary key column names for a specific layer.
    Used for identifying unique features in the frontend or constructing PK-based queries.
    """
    with LayerInfo.managed_session() as session:
        # We use selectinload to ensure layer_accessibility is loaded efficiently
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return Response(status_code=204)

    # Access the pk_cols list from the accessibility model
    pk_cols = info.layer_accessibility.pk_cols or []

    return {
        "pkCols": pk_cols,
        "meta": {
            "uuid": uuid,
            "count": len(pk_cols)
        }
    }