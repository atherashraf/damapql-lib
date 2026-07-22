from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.schemas.layer_query import (
    UpdateColumnValueRequest,
    FeatureByPkRequest,
    ColumnValueRequest,
)
from app.damapql.dch.services.layer_query_services import (
    LayerQueryService,
    LayerLookupBy,
)

layer_query_router = APIRouter(tags=["layer-query"])


# ============================================================================
# Tree / Navigation Query
# ============================================================================

@layer_query_router.get("/layer-query/categories")
def get_layer_categories(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get all layer categories.

    Endpoint:
    GET /api/dch/layer-query/categories

    Returns:
    - List of categories (main + sub)

    Use case:
    - Populate category dropdown
    """
    return {"payload": LayerQueryService.get_categories()}


@layer_query_router.get("/layer-query/layers")
def get_layers(
    app_label: str | None = Query(default=None),
    category_id: int | None = Query(default=None),
    data_model: str | None = Query(default=None),
    model_type: str | None = Query(default=None),
    title: str | None = Query(default=None),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get flat list of layers with optional filters.

    Endpoint:
    GET /api/dch/layer-query/layers

    Query params:
    - app_label: filter by app/module
    - category_id: filter by category
    - data_model: "V" (vector) or "R" (raster)
    - model_type: e.g. "DB"
    - title: partial match

    Returns:
    - List of layers with metadata

    Use case:
    - Layer selection UI
    """
    payload = LayerQueryService.get_layers(
        app_label=app_label,
        category_id=category_id,
        data_model=data_model,
        model_type=model_type,
        title=title,
    )
    return {"payload": payload}


@layer_query_router.get("/layer-query/tree")
def get_layer_tree(
    app_label: str | None = Query(default=None),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get hierarchical tree of layers grouped by category.

    Endpoint:
    GET /api/dch/layer-query/tree

    Query params:
    - app_label (optional)

    Returns:
    - Nested structure:
        main -> sub -> layers

    Use case:
    - Tree view UI
    """
    return {"payload": LayerQueryService.get_layer_tree(app_label=app_label)}


# ============================================================================
# Metadata Query
# ============================================================================

@layer_query_router.get("/layer-query/{identifier}/meta")
def get_layer_meta(
    identifier: str,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get layer metadata.

    Endpoint:
    GET /api/dch/layer-query/{identifier}/meta

    Params:
    - identifier: uuid | layer_name | table_name
    - by: lookup type

    Returns:
    - Full layer metadata (category, accessibility, etc.)

    Use case:
    - Load layer config for UI or processing
    """
    return {"payload": LayerQueryService.get_layer_meta(identifier=identifier, by=by)}


@layer_query_router.get("/layer-query/{identifier}/fields")
def get_layer_fields(
    identifier: str,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get layer field definitions.

    Endpoint:
    GET /api/dch/layer-query/{identifier}/fields

    Params:
    - identifier: uuid | layer_name | table_name
    - by: lookup type

    Returns:
    - List of column metadata

    Use case:
    - Build forms / filters dynamically
    """
    return {"payload": LayerQueryService.get_layer_fields(identifier=identifier, by=by)}


@layer_query_router.get("/layer-query/{identifier}/distinct-values")
def get_distinct_values(
    identifier: str,
    col_name: str = Query(..., description="Column name to fetch unique values for"),
    by: LayerLookupBy = Query(default="uuid"),
    where_clause: str | None = Query(default=None),
    limit: int = Query(
        default=500,
        le=5000,
        description="Max values to return. Use -1 for all."
    ),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get distinct values for a column.

    Endpoint:
    GET /api/dch/layer-query/{identifier}/distinct-values

    Params:
    - identifier: uuid | layer_name | table_name
    - by: lookup type
    - col_name: column to fetch distinct values
    - where_clause (optional): SQL filter
    - limit: max values (default 5000) -1 mean all

    Example:
    - Get tehsils of district 1:
      where_clause = "dist_id = 1"

    Returns:
    - List of unique values

    Use case:
    - Dropdown filters (dependent filtering supported)
    """
    values = LayerQueryService.get_field_distinct_values(
        identifier=identifier,
        by=by,
        field_name=col_name,
        where_clause=where_clause,
        limit=limit,
    )
    return {"payload": values}


# ============================================================================
# Attribute / Feature Query
# ============================================================================

@layer_query_router.get("/layer-query/{identifier}/attributes")
def get_layer_attributes(
    identifier: str,
    by: LayerLookupBy = Query(default="uuid"),
    cols: list[str] | None = Query(default=None),
    where_clause: str | None = Query(default=None),
    limit: int = Query(default=-1, ge=-1),
    offset: int = Query(default=0, ge=0),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get attribute table data.

    Endpoint:
    GET /api/dch/layer-query/{identifier}/attributes

    Params:
    - identifier: uuid | layer_name | table_name
    - by: lookup type
    - cols: list of columns (repeat param)
    - where_clause: SQL filter
    - limit:
        -1 → ALL rows
        >=0 → paginated
    - offset: pagination offset

    Example:
    - Get all tehsils of district 1:
      where_clause = "dist_id = 1"

    Returns:
    - columns
    - rows
    - total
    - pkCols

    Use case:
    - Table view
    - Data export
    """
    payload = LayerQueryService.get_layer_attributes(
        identifier=identifier,
        by=by,
        cols=cols,
        where_clause=where_clause,
        limit=limit,
        offset=offset,
    )
    return {"payload": payload}


@layer_query_router.post("/layer-query/{identifier}/feature-detail")
def get_feature_detail(
    identifier: str,
    payload: FeatureByPkRequest,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get a single feature (row) by primary key values.

    Endpoint:
    POST /api/dch/layer-query/{identifier}/feature-detail

    Body:
    - pk_values: list[str]

    Returns:
    - Single row (geometry removed)

    Use case:
    - Feature detail popup
    """
    result = LayerQueryService.get_feature_detail(
        identifier=identifier,
        by=by,
        pk_values=payload.pk_values,
    )

    if isinstance(result, dict):
        return {"payload": result}
    return result


@layer_query_router.post("/layer-query/{identifier}/column-value")
def get_column_value(
    identifier: str,
    payload: ColumnValueRequest,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get a single column value from a row.

    Endpoint:
    POST /api/dch/layer-query/{identifier}/column-value

    Body:
    - pk_values
    - col_name

    Returns:
    - {col_name: value}

    Use case:
    - Inline field fetch
    """
    value = LayerQueryService.get_column_value(
        identifier=identifier,
        by=by,
        pk_values=payload.pk_values,
        col_name=payload.col_name,
    )
    return {"payload": {payload.col_name: value}}


@layer_query_router.post("/layer-query/{identifier}/update-column-value")
def update_column_value(
    identifier: str,
    payload: UpdateColumnValueRequest,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Update a column value for a row.

    Endpoint:
    POST /api/dch/layer-query/{identifier}/update-column-value

    Body:
    - pk_values
    - col_name
    - value

    Returns:
    - updated count
    - column name
    - new value

    Use case:
    - Inline editing
    """
    result = LayerQueryService.update_column_value(
        identifier=identifier,
        by=by,
        pk_values=payload.pk_values,
        col_name=payload.col_name,
        value=payload.value,
    )
    return {"payload": result}

@layer_query_router.get("/layer-query/{identifier}/feature-extent")
def get_feature_extent(
    identifier: str,
    pk: str,
    by: LayerLookupBy = Query(default="table_name"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    return {"payload": LayerQueryService.get_feature_extent(identifier=identifier, by=by, pk=pk)}

@layer_query_router.get("/layer-query/{identifier}/feature-geojson")
def get_feature_geojson(
    identifier: str,
    pk: str,
    by: LayerLookupBy = Query(default="uuid"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Get a single feature as GeoJSON by primary key values.

    Endpoint:
    POST /api/dch/layer-query/{identifier}/feature-geojson

    Body:
    - pk_values: list[str]

    Returns:
    - GeoJSON Feature

    Use case:
    - Map highlight
    - Boundary navigation
    - Zoom to selected feature
    """
    feature = LayerQueryService.get_features_geojson(
        identifier=identifier,
        by=by,
        pk_values=[pk]
    )
    return {"payload": feature}