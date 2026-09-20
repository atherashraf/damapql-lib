import math

import numpy as np
import pandas as pd
from sqlalchemy.orm import selectinload

from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import case, func, literal, select
from sqlalchemy.orm.attributes import flag_modified

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.api_enum import DataModel, ApplicationURLType
from app.damapql.dch.dch_map_schema import MapSaveRequest
from app.damapql.dch.dtypes import LayerInfoListResponse, MapInfoResponse
from app.damapql.dch.models import LayerInfo, LayerAccessibility, LayerCategory, MapInfo, DBQuery
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils


dch_maps_router = APIRouter(tags=["dch"])


def require_dch_admin(current_user: UserInfo):
    if getattr(current_user, "is_superuser", False):
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required",
    )


@dch_maps_router.get(
    "/all_layer_info/",
    response_model=LayerInfoListResponse,
    summary="Get All Layer Info"
)
async def get_all_layer_info(current_user: UserInfo =
                             Depends(AuthUtils.get_current_user)):
    require_dch_admin(current_user)
    with LayerInfo.managed_session() as session:
        category_label = case(
            (LayerCategory.sub.is_(None), LayerCategory.main),
            else_=func.concat(LayerCategory.main, literal(" - "), LayerCategory.sub)
        ).label("category")

        stmt = (
            select(
                LayerInfo.id,
                LayerInfo.uuid,
                LayerInfo.title,
                LayerInfo.layer_name,
                category_label,
                LayerAccessibility.accessibility_type,
                LayerAccessibility.params["table_name"].as_string().label("table_name"),
                LayerInfo.srid,
                LayerInfo.extent,
                LayerInfo.extent_3857,
                LayerInfo.data_model,
                LayerInfo.model_type,
                LayerInfo.geom_type,
                LayerInfo.zoom_range,
                LayerInfo.layer_styling,
                LayerInfo.data_url,
                LayerInfo.date_range_url,

            )
            .join(LayerAccessibility, LayerAccessibility.id == LayerInfo.layer_access_id)
            .join(LayerCategory, LayerCategory.id == LayerInfo.category_id, isouter=True)
        )

        df = DBQuery.execute_stmt_as_df(
            session,
            stmt,
            enum_columns=["data_model", "model_type", "accessibility_type", "geom_type"]
        )
        # enforce exact order from select list
        column_order = [
            "id",
            "uuid",
            "title",
            "layer_name",
            "category",
            "accessibility_type",
            "table_name",
            "srid",
            "extent",
            "extent_3857",
            "data_model",
            "model_type",
            "geom_type",
            "zoom_range",
            "layer_styling",
            "data_url",
            "date_range_url",

        ]
        df = df[column_order]
    return {"payload": ManageDataFrameUtils.grid_response(df, pkCols=["uuid"])}

@dch_maps_router.get("/all_map_info/")
async def get_all_map_info(current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    df = MapInfo.get_all_map_info()
    return {"payload": ManageDataFrameUtils.grid_response(df, pkCols=["uuid"])}


@dch_maps_router.post("/save_map/")
async def save_map(
    payload: MapSaveRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_dch_admin(current_user)

    with MapInfo.managed_session() as session:
        map_info = None
        existing_styles = {}

        if payload.uuid != "-1":
            map_info = session.execute(
                select(MapInfo).where(MapInfo.uuid == payload.uuid)
            ).scalar_one_or_none()

            if map_info:
                existing_styles = {
                    l.get("uuid"): l.get("style")
                    for l in (map_info.layers or [])
                    if isinstance(l, dict) and "style" in l
                }

        if not map_info:
            map_info = MapInfo(
                uuid=DCHUtils.get_uuid1() if payload.uuid == "-1" else payload.uuid
            )

        layers = []

        # Base layer
        layers.append({
            "uuid": "-1",
            "isBase": True,
            "key": payload.baseLayer.strip(),
        })

        # DA layers
        for da in payload.daLayers:
            da_uuid = da.uuid.strip()
            style = da.style if da.style else existing_styles.get(da_uuid)

            layers.append({
                "uuid": da_uuid,
                "isBase": False,
                "style": style,
                "visible": da.visible,
                "zoomRange": [0, 30],
                "opacity": da.opacity,
                "zIndex": da.zIndex,
                "title": da.title,
                "name": da.name,
                "groupKey": da.groupKey,
                "groupTitle": da.groupTitle,
            })

        # WMS / other layers
        for info in payload.otherLayers:
            layers.append({
                "uuid": info.uuid,
                "isBase": False,
                "name": info.name.strip(),
                "key": info.title or info.name,
                "title": info.title or info.name,
                "params": info.params,
                "type": info.type,
                "visible": info.visible,
                "opacity": info.opacity,
                "zIndex": info.zIndex,
                "groupKey": info.groupKey,
                "groupTitle": info.groupTitle,
            })

        # Add group metadata
        if payload.groups:
            layers.append({
                "uuid": "-1",
                "isGroupMeta": True,
                "groups": [g.model_dump() for g in payload.groups],
            })

        map_info.title = payload.mapName
        map_info.layers = layers
        map_info.extent = payload.extent
        map_info.srid = 3857
        map_info.units = "m"

        flag_modified(map_info, "extent")
        flag_modified(map_info, "layers")
        # flag_modified(map_info, "groups")

        map_info.save(session=session)

        return {
            "payload": {
                "msg": "Map saved successfully",
                "uuid": map_info.uuid,
            }
        }


@dch_maps_router.get(
    "/get_map_info/{uuid}/",
    response_model=MapInfoResponse,
    summary="Get Map Info by UUID",
)
async def get_map_info(uuid: str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    with MapInfo.managed_session() as session:
        map_obj = session.execute(
            select(MapInfo).where(MapInfo.uuid == uuid)
        ).scalar_one_or_none()

        if map_obj:
            map_info = map_obj.to_object()
            return {"payload": map_info}

    return Response(status_code=204)


@dch_maps_router.delete("/delete_map/{uuid}/")
async def delete_map(uuid: str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    require_dch_admin(current_user)

    with MapInfo.managed_session() as session:
        obj = session.execute(
            select(MapInfo).where(MapInfo.uuid == uuid)
        ).scalar_one_or_none()

        if obj:
            session.delete(obj)
            session.commit()
            return {"payload": {"msg": "Map Info deleted"}}

    return Response(status_code=204)


# @dch_maps_router.delete("/delete_layerinfo_row/{uuid}/")
# async def delete_layer_info_row(uuid: str,
#                                 current_user: UserInfo = Depends(AuthUtils.get_current_user)):
#     require_dch_admin(current_user)
#
#     with LayerInfo.managed_session() as session:
#         obj = session.execute(
#             select(LayerInfo).where(LayerInfo.uuid == uuid)
#         ).scalar_one_or_none()
#
#         if obj:
#             session.delete(obj)
#             session.commit()
#             return {"payload": {"msg": "Layer Info deleted"}}
#
#     return Response(status_code=204)

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse


@dch_maps_router.delete("/delete_layerinfo_row/{uuid}/")
async def delete_layer_info_row(uuid: str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    require_dch_admin(current_user)

    with LayerInfo.managed_session() as session:
        layer = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if not layer:
            raise HTTPException(status_code=404, detail="Layer Info not found")

        # Delete both explicitly
        if layer.layer_accessibility:
            session.delete(layer.layer_accessibility)
        session.delete(layer)
        session.commit()

        # 4. (Optional) Purge layer from MapInfo JSON configs
        # session.execute(
        #     text("""
        #         UPDATE dch_map_info
        #         SET layers = (
        #             SELECT jsonb_agg(elem)
        #             FROM jsonb_array_elements(layers::jsonb) elem
        #             WHERE elem->>'uuid' != :uuid
        #         )
        #         WHERE layers::text LIKE :like_pattern
        #     """),
        #     {"uuid": uuid, "like_pattern": f"%{uuid}%"}
        # )

        session.commit()

    return {"payload": {"msg": f"Layer {uuid} deleted successfully"}}



def make_json_safe(value):
    if value is None:
        return None

    # numpy arrays
    if isinstance(value, np.ndarray):
        return [make_json_safe(v) for v in value.tolist()]

    # lists / tuples / sets
    if isinstance(value, (list, tuple, set)):
        return [make_json_safe(v) for v in value]

    # dictionaries
    if isinstance(value, dict):
        return {
            str(k): make_json_safe(v)
            for k, v in value.items()
        }

    # numpy / Python floating point
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None

    # numpy integer
    if isinstance(value, np.integer):
        return int(value)

    # numpy bool
    if isinstance(value, np.bool_):
        return bool(value)

    # pandas missing values
    try:
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and missing:
            return None
    except (TypeError, ValueError):
        pass

    return value

@dch_maps_router.get("/get_all_layers/")
async def get_all_layers(
    current_user: UserInfo = Depends(AuthUtils.get_current_user)
):
    with LayerInfo.managed_session() as session:
        stmt = (
            select(
                LayerInfo.title,
                LayerInfo.layer_name,
                LayerInfo.uuid,
                LayerInfo.extent_3857,
                LayerCategory.main.label("category"),
            )
            .join(
                LayerCategory,
                LayerCategory.id == LayerInfo.category_id,
                isouter=True,
            )
            .where(
                LayerInfo.data_model != DataModel.Application.name
            )
        )

        df = DBQuery.execute_stmt_as_df(session, stmt)

        records = df.to_dict(orient="records")
        payload = make_json_safe(records)

        da_logger.info(f"get_all_layers: {payload}")

        return {"payload": payload}


@dch_maps_router.get("/get_application_url/{url_type}/")
async def get_application_layers(
    url_type: ApplicationURLType,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    with LayerInfo.managed_session() as session:
        stmt = (
            select(
                LayerInfo.title,
                LayerInfo.layer_name,
                LayerInfo.uuid,
                LayerAccessibility.url,
                LayerAccessibility.url_type,
            )
            .join(LayerAccessibility, LayerInfo.layer_access_id == LayerAccessibility.id)
            .where(LayerInfo.data_model == DataModel.Application.name)
            .where(LayerAccessibility.url_type == url_type.value)
        )

        df = DBQuery.execute_stmt_as_df(session, stmt)
        records = df.to_dict(orient="records")

        da_logger.info(f"get_application_layers: {records}")
        return {"payload": records}


@dch_maps_router.get("/layer_info/{uuid}/")
async def get_layer_info(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user)
):
    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if info:
            return {
                "payload": {
                    "uuid": info.uuid,
                    "title": info.title,
                    "dataModel": info.data_model.code if info.data_model else None,
                    "geomType": list(info.geom_type) if info.geom_type is not None else [],
                    "zoomRange": info.zoom_range or [0, 30],
                    "extent3857": info.extent_3857,
                    "style": info.layer_styling,
                    "dataURL": info.data_url,
                    "layerSetting": info.layer_setting or {},
                    "format": info.format,
                    "dateRangeURL": info.date_range_url,
                }
            }

    return Response(status_code=204)


MAX_3857 = 20037508.342789244

def is_valid_3857_extent(extent) -> bool:
    if not isinstance(extent, (list, tuple)) or len(extent) != 4:
        return False
    try:
        min_x, min_y, max_x, max_y = [float(v) for v in extent]
    except (ValueError, TypeError):
        return False

    # Check finite numbers, positive dimensions, and Web Mercator bounds
    if not (min_x < max_x and min_y < max_y):
        return False
    if any(abs(v) > MAX_3857 for v in (min_x, min_y, max_x, max_y)):
        return False

    return True


@dch_maps_router.get("/layer_extent/{uuid}/")
async def get_layer_extent(uuid: str):
    with LayerInfo.managed_session() as session:
        extent = session.execute(
            select(LayerInfo.extent_3857).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if is_valid_3857_extent(extent):
            return {"payload": extent}

        # Fallback to default configured bounds
        _, fallback_3857 = DCHUtils.get_default_extents(3857)
        return {"payload": fallback_3857}

@dch_maps_router.get("/get_map_uuid/{layer_title}/")
def get_map_uuid(layer_title:str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    with MapInfo.managed_session() as session:
        qs = select(MapInfo.uuid).where(MapInfo.title == layer_title)
        res = session.execute(qs).scalar_one_or_none()
        if res is not None:
            return {"payload": res}
        return Response(status_code=204)

@dch_maps_router.get("/get_default_extent/",
    summary="Get Application Default Extent")
def get_default_extent():
    native_ext, ext_3857 = DCHUtils.get_default_extents(3857)
    return {"native_extent": native_ext, "extent_3857": ext_3857}