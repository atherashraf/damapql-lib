import os
import shutil

import geopandas as gpd
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
from app.dch.models import (
    AccessibilityType,
    DBConnection,
    DBQuery,
    DataModel,
    LayerAccessibility,
    LayerCategory,
    LayerInfo,
)
from app.dch.utils.dch_utils import DCHUtils
from settings import DEFAULT_DB_KEY, MEDIA_DIR
from utils.adapters.db import DBBase
from utils.adapters.model_utils import ModelUtils
from utils.singletons import da_logger

dch_admin_router = APIRouter(tags=["dch"])


def require_dch_admin(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


@dch_admin_router.get("/get_db_connection/")
async def get_db_connection(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Returns available DB connections.
    Protected endpoint.
    """
    require_dch_admin(current_user)

    with DBConnection.managed_session() as session:
        stmt = select(DBConnection.id, DBConnection.name)
        df = DBQuery.execute_stmt_as_df(session, stmt)
        return {"payload": df.to_dict(orient="records")}


@dch_admin_router.get("/layer_categories/")
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


@dch_admin_router.post("/add_model_row/")
async def add_model_row(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Adds a model row dynamically using ModelUtils.
    Protected endpoint.
    """
    require_dch_admin(current_user)

    form = await request.form()
    model_name = form.get("model_name")

    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")

    row = {}
    for key in form.keys():
        if key != "model_name":
            row[key] = form.get(key)

    obj = ModelUtils.add_model_obj(model_name, row)
    if obj is not None:
        return {"payload": {"msg": "row added"}}

    return Response(status_code=400)


@dch_admin_router.get("/add_layer_url_info/{layer_title}/{layer_category_id}/{layer_url}/{url_type}/")
async def add_layer_url_info(
    layer_title: str,
    layer_category_id: str,
    layer_url: str,
    url_type: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Adds a URL-based layer metadata entry.
    Protected endpoint.
    """
    require_dch_admin(current_user)

    with LayerInfo.managed_session() as session:
        accessibility = LayerAccessibility(
            accessibility_type=AccessibilityType.URL.name,
            params={
                "url": layer_url,
                "url_type": url_type,
            }
        )
        session.add(accessibility)
        session.flush()

        layer = LayerInfo(
            uuid=DCHUtils.get_uuid1(),
            title=layer_title,
            layer_name=layer_title.replace(" ", "_"),
            data_model=DataModel.URL.name,
            category_id=int(layer_category_id),
            layer_access_id=accessibility.id,
        )

        session.add(layer)
        session.commit()

    return {"payload": {"msg": "row added"}}


@dch_admin_router.post("/save_layer_info/")
async def save_layer_info(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Updates LayerInfo row fields dynamically.
    Protected endpoint.
    """
    require_dch_admin(current_user)

    data = await request.json()
    pk_col = data.get("pk", {}).get("colName")
    pk_val = data.get("pk", {}).get("colValue")
    row_data = data.get("rowData")

    if not pk_col or pk_val is None or not isinstance(row_data, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    with LayerInfo.managed_session() as session:
        obj = session.execute(
            select(LayerInfo).where(getattr(LayerInfo, pk_col) == pk_val)
        ).scalar_one_or_none()

        if not obj:
            raise HTTPException(status_code=404, detail="LayerInfo not found")

        for key, value in row_data.items():
            if hasattr(obj, key):
                if key == "data_model":
                    value = DataModel.get_name(value)
                setattr(obj, key, value)

        obj.save(session=session, allow_update=True)

    return {"status": "success", "updated": row_data}


@dch_admin_router.post("/upload_shp_file/")
async def upload_shp_file(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Upload shapefile parts, import into DB, and register the new vector layer.
    Protected endpoint.
    """
    require_dch_admin(current_user)

    form = await request.form()
    layer_category_id = form.get("layer_category_id")

    upload_files = [value for _, value in form.items() if hasattr(value, "filename")]
    if not upload_files:
        raise HTTPException(status_code=400, detail="No files provided")

    filename_without_ext = None
    file_dir = None

    for file in upload_files:
        filename = file.filename

        if file_dir is None:
            filename_without_ext = os.path.splitext(filename)[0]
            file_dir = os.path.join(MEDIA_DIR, "uploads", filename_without_ext)
            os.makedirs(file_dir, exist_ok=True)

        file_path = os.path.join(file_dir, filename)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

    shp_fp = os.path.join(file_dir, f"{filename_without_ext}.shp")
    if not os.path.exists(shp_fp):
        raise HTTPException(status_code=400, detail="Missing .shp file in upload")

    gdf = gpd.read_file(shp_fp)
    gdf.rename_geometry("geom", inplace=True)
    gdf.columns = gdf.columns.str.lower()

    cols_to_drop = [c for c in ["objectid", "fid", "id"] if c in gdf.columns]
    if cols_to_drop:
        gdf.drop(columns=cols_to_drop, inplace=True)

    uuid = DCHUtils.get_uuid1()
    table_name = DCHUtils.short_table_name(filename_without_ext)
    engine = DBBase.get_engine(DEFAULT_DB_KEY)

    try:
        gdf.to_postgis(table_name, con=engine, if_exists="replace", index=False)

        DCHUtils.create_vector_layer_info(
            DEFAULT_DB_KEY,
            table_name,
            uuid,
            layer_category_id=layer_category_id
        )

    except Exception as e:
        da_logger.exception(f"PostGIS import error during shapefile upload: {e}")
        raise HTTPException(status_code=500, detail="Failed to import spatial data to database")

    return {
        "payload": {
            "msg": "Shapefile uploaded and imported successfully",
            "uuid": uuid,
            "table": table_name,
        }
    }


"""
Two notes for your codebase:

if your LayerInfo model really uses category instead of category_id, change that one field accordingly in add_layer_url_info

if AccessibilityType.URL.name is not valid in your enum/model setup, replace it with the exact stored value your project uses
"""