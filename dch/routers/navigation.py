import json

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import MapInfo, DBQuery
from utils.adapters.db import DBBase
from utils.singletons import da_logger

dch_navigation_router = APIRouter(tags=["dch"])


@dch_navigation_router.get("/navigation_geometry/{map_uuid}/{selected_key}/{node_id}/")
async def get_navigation_geometry(
    map_uuid: str,
    selected_key: str,
    node_id: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns geometry for a navigation node from the external DB
    configured in the map's navigation_setting.
    """
    with MapInfo.managed_session() as session:
        map_info = session.execute(
            select(MapInfo).where(MapInfo.uuid == map_uuid)
        ).scalar_one_or_none()

    if not map_info or not map_info.navigation_setting:
        return Response(status_code=204)

    nav_setting = map_info.navigation_setting.get(selected_key)
    if not nav_setting:
        return Response(status_code=204)

    try:
        target_engine = DBBase.get_engine(nav_setting["db_key"])

        geom_col = nav_setting["geom_col"]
        table = nav_setting["table"]

        query = f"""
            SELECT id, ST_Transform("{geom_col}", 4326) as geom
            FROM {table}
            WHERE id = :node_id
        """

        with Session(target_engine) as target_session:
            gdf = DBQuery.execute_query_as_gdf(
                target_session,
                query,
                params={"node_id": node_id},
                geom_col="geom",
                crs="EPSG:4326",
            )

        if gdf.empty:
            return Response(status_code=204)

        geojson_str = gdf.to_json()
        return {"payload": json.loads(geojson_str)}

    except Exception as e:
        da_logger.exception(
            f"Failed to fetch navigation geometry for map_uuid={map_uuid}, selected_key={selected_key}, node_id={node_id}: {e}"
        )
        return Response(status_code=500)


@dch_navigation_router.get("/navigation_list/{map_uuid}/")
async def get_navigation_list(
    map_uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns hierarchical navigation data for the given map.
    Data sources are defined in map_info.navigation_setting.
    """
    with MapInfo.managed_session() as session:
        map_info = session.execute(
            select(MapInfo).where(MapInfo.uuid == map_uuid)
        ).scalar_one_or_none()

    if not map_info or not map_info.navigation_setting:
        return Response(status_code=204)

    final_res = {}

    for key, setting in map_info.navigation_setting.items():
        try:
            res = {"name": setting["name"]}
            target_engine = DBBase.get_engine(setting["db_key"])
            table_name = setting["table"]

            query = f"""
                SELECT id, name, level, parent, "parentId"
                FROM {table_name}
                ORDER BY level, name
            """

            with Session(target_engine) as target_session:
                df = DBQuery.execute_stmt_as_df(target_session, query)

            res["data"] = df.to_dict(orient="records")
            final_res[key] = res

        except Exception as e:
            da_logger.exception(
                f"Navigation error for map_uuid={map_uuid}, key={key}, table={setting.get('table')}: {e}"
            )
            continue

    return {"payload": final_res}