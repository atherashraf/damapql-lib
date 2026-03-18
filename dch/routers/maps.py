from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import case, func, literal, select

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
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
async def get_all_layer_info(current_user: UserInfo = Depends(AuthUtils.get_current_user)):
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
                LayerAccessibility.params["table_name"].as_string().label("table_name"),
                LayerInfo.srid,
                LayerInfo.extent,
                LayerInfo.extent_3857,
                LayerInfo.data_model,
                LayerInfo.geom_type,
                LayerInfo.zoom_range,
                LayerInfo.layer_styling,
                LayerInfo.data_url,
                LayerInfo.date_range_url,
            )
            .join(LayerAccessibility, LayerAccessibility.id == LayerInfo.layer_access_id)
            .join(LayerCategory, LayerCategory.id == LayerInfo.category_id, isouter=True)
        )

        df = DBQuery.execute_stmt_as_df(session, stmt, enum_columns=["data_model"])

    return ManageDataFrameUtils.grid_response(df, pkCols=["uuid"])

@dch_maps_router.get("/all_map_info/")
async def get_all_map_info(current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    df = MapInfo.get_all_map_info()
    return ManageDataFrameUtils.grid_response(df, pkCols=["uuid"])


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
        for da in payload.daLayers:
            style = da.style.model_dump() if da.style else existing_styles.get(da.uuid.strip())
            layers.append({
                "uuid": da.uuid.strip(),
                "style": style,
                "visible": da.visible,
                "zoomRange": [0, 30],
                "opacity": da.opacity,
            })

        layers.append({
            "uuid": "-1",
            "isBase": True,
            "key": payload.baseLayer.strip()
        })

        for info in payload.otherLayers:
            layers.append({
                "uuid": "-1",
                "isBase": False,
                "key": info.name.strip(),
                "params": info.params,
                "type": info.type,
                "visible": True,
            })

        map_info.title = payload.mapName
        map_info.layers = layers
        map_info.extent = payload.extent
        map_info.srid = 3857
        map_info.units = "m"
        map_info.save(session=session)

        return {"payload": {"msg": "Map saved successfully", "uuid": map_info.uuid}}


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
            return {"payload": map_obj.to_object()}

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


@dch_maps_router.delete("/delete_layerinfo_row/{uuid}/")
async def delete_layer_info_row(uuid: str,
                                current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    require_dch_admin(current_user)

    with LayerInfo.managed_session() as session:
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if obj:
            session.delete(obj)
            session.commit()
            return {"payload": {"msg": "Layer Info deleted"}}

    return Response(status_code=204)


@dch_maps_router.get("/get_all_layers/")
async def get_all_layers(current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    with LayerInfo.managed_session() as session:
        stmt = (
            select(
                LayerInfo.title,
                LayerInfo.layer_name,
                LayerInfo.uuid,
                LayerInfo.extent_3857,
                LayerCategory.main.label("category"),
            )
            .join(LayerCategory, LayerCategory.id == LayerInfo.category_id, isouter=True)
        )

        df = DBQuery.execute_stmt_as_df(session, stmt)
        da_logger.info(f"get_all_layers: {df.to_dict(orient='records')}")
        return {"payload": df.to_dict(orient="records")}


@dch_maps_router.get("/layer_info/{uuid}/")
async def get_layer_info(uuid: str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
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


@dch_maps_router.get("/layer_extent/{uuid}/")
async def get_layer_extent(uuid: str, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    with LayerInfo.managed_session() as session:
        extent = session.execute(
            select(LayerInfo.extent_3857).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if extent is not None:
            return {"payload": extent}

    return Response(status_code=204)