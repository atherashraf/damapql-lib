import io
import json
import os.path
import re
import shutil
import geopandas as gpd
from datetime import datetime

from typing import List

import jenkspy
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from matplotlib import pyplot as plt, colorbar
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from sqlalchemy import case, literal, text

from starlette.responses import JSONResponse

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.models import User

from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.api_enum import DataModel, ModelType, AccessibilityType

from app.damapql.dch.dch_map_schema import MapSaveRequest
from app.damapql.dch.dtypes import LayerInfoListResponse, MapInfoResponse, AttributeTableResponse, \
    UploadRasterResponse
from app.damapql.dch.models import LayerCategory, LayerInfo, MapInfo, LayerAccessibility, DBConnection

from app.damapql.dch.utils.email_utils import EmailRequest, send_email

from fastapi import APIRouter,  HTTPException, UploadFile, File, Form, Depends
from starlette.requests import Request

from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from utils.adapters.db import DBBase

from utils.adapters.deps import get_sync_session

from utils.adapters.model_utils import ModelUtils
from utils.adapters.services.db_query import DBQuery
from utils.singletons import da_logger

from settings import MEDIA_DIR, APP_NAME, DATA_DB_KEY

import pandas as pd
from typing import Optional, Union
from sqlalchemy import func

dch_router: APIRouter = APIRouter(
    tags=["dch"],
    responses={404: {"description": "Not found"}},
)


@dch_router.get(
    "/all_layer_info/",
    response_model=LayerInfoListResponse,
    summary="Get All Layer Info"
)
async def get_all_layer_info(user: User = Depends(AuthUtils.get_current_user)):
    """
    Returns metadata for all layers using high-performance DBQuery.
    """
    with LayerInfo.managed_session() as session:
        category_label = case(
            (LayerCategory.sub.is_(None), LayerCategory.main),
            else_=func.concat(LayerCategory.main, literal(" - "), LayerCategory.sub)
        ).label("category")

        stmt = (
            select(
                LayerInfo.id, LayerInfo.uuid, LayerInfo.title, LayerInfo.layer_name,
                category_label,
                LayerAccessibility.params["table_name"].astext.label("table_name"),
                LayerInfo.srid, LayerInfo.extent, LayerInfo.extent_3857,
                LayerInfo.data_model, LayerInfo.geom_type, LayerInfo.zoom_range,
                LayerInfo.layer_styling, LayerInfo.data_url, LayerInfo.date_range_url,
            )
            .join(LayerAccessibility, LayerAccessibility.id == LayerInfo.layer_access_id)
            .join(LayerCategory, LayerCategory.id == LayerInfo.category_id, isouter=True)
        )

        df = DBQuery.execute_stmt_as_df(session, stmt, enum_columns=["data_model"])

    return ManageDataFrameUtils.grid_response(df, pkCols=["uuid"])


@dch_router.get("/all_map_info/")
async def get_all_map_info():
    df = MapInfo.get_all_map_info()
    payload = ManageDataFrameUtils.grid_response(df, pkCols=['uuid'])
    return payload


@dch_router.post("/save_map/")
async def save_map(
        payload: MapSaveRequest,
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Unified Map Save logic.
    1. Validates Superuser.
    2. Merges incoming styles with existing ones if not provided.
    3. Uses Pydantic for safety.
    """
    if not AuthUtils.has_role(current_user, "superuser"):
        raise HTTPException(status_code=403, detail="Superuser privileges required")

    with MapInfo.managed_session() as session:
        map_info = None
        existing_styles = {}

        if payload.uuid != "-1":
            stmt = select(MapInfo).where(MapInfo.uuid == payload.uuid)
            map_info = session.execute(stmt).scalar_one_or_none()
            if map_info:
                existing_styles = {
                    l.get("uuid"): l.get("style")
                    for l in (map_info.layers or [])
                    if isinstance(l, dict) and "style" in l
                }

        if not map_info:
            map_info = MapInfo(uuid=DCHUtils.get_uuid1() if payload.uuid == "-1" else payload.uuid)

        # Build Layer stack
        layers = []
        for da in payload.daLayers:
            # Fallback to existing style if payload style is null
            style = da.style.model_dump() if da.style else existing_styles.get(da.uuid.strip())
            layers.append({
                "uuid": da.uuid.strip(),
                "style": style,
                "visible": da.visible,
                "zoomRange": [0, 30],
                "opacity": da.opacity,
            })

        layers.append({"uuid": "-1", "isBase": True, "key": payload.baseLayer.strip()})
        for info in payload.otherLayers:
            layers.append({
                "uuid": "-1", "isBase": False, "key": info.name.strip(),
                "params": info.params, "type": info.type, "visible": True
            })

        map_info.title = payload.mapName
        map_info.layers = layers
        map_info.extent = payload.extent
        map_info.srid = 3857
        map_info.units = "m"
        map_info.save(session=session, allow_update=True)

    return {"payload": {"msg": "Map saved successfully", "uuid": map_info.uuid}}


@dch_router.get(
    "/get_map_info/{uuid}/",
    response_model=MapInfoResponse,
    summary="Get Map Info by UUID",
    description="Retrieves the saved map configuration for a given UUID, including layer list and extent."
)
async def get_map_info(uuid: str):
    """
    Fetches map metadata using the managed session pattern.
    """
    with MapInfo.managed_session() as session:
        # 1. Use modern select syntax
        stmt = select(MapInfo).where(MapInfo.uuid == uuid)
        map_obj = session.execute(stmt).scalar_one_or_none()

        # 2. Handle the result
        if map_obj:
            # Assuming to_object() is your serialization helper
            return {"payload": map_obj.to_object()}

    # 3. Return 204 No Content if not found (standard for your current API design)
    return Response(status_code=204)


@dch_router.get("/delete_map/{uuid}/")
async def delete_map(uuid: str):
    """
    Deletes a MapInfo record by UUID using the Mixin's managed session.
    """
    # Use the Mixin's managed_session directly on the class
    with MapInfo.managed_session() as session:
        # Create a select statement for the specific UUID
        stmt = select(MapInfo).where(MapInfo.uuid == uuid)
        obj = session.execute(stmt).scalar_one_or_none()

        if obj is not None:
            session.delete(obj)
            session.commit()
            return {"payload": {"msg": "Map Info deleted"}}

    # Return 204 No Content if the map didn't exist
    return Response(status_code=204)


@dch_router.get("/delete_layerinfo_row/{uuid}/")
async def delete_layer_info_row(uuid: str):
    """
    Deletes a LayerInfo record and its associated accessibility data.
    Uses the managed_session pattern for automatic cleanup.
    """
    with LayerInfo.managed_session() as session:
        # Modern SQLAlchemy 2.0 selection
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        obj = session.execute(stmt).scalar_one_or_none()

        if obj is not None:
            session.delete(obj)
            session.commit()
            return {"payload": {"msg": "Layer Info deleted"}}

    # If no object was found to delete
    return Response(status_code=204)


@dch_router.get("/get_all_layers/")
async def get_all_layers():
    """
    Optimized list for layer selection dropdowns.
    """
    with LayerInfo.managed_session() as session:
        stmt = select(
            LayerInfo.title, LayerInfo.layer_name, LayerInfo.uuid,
            LayerInfo.extent_3857, LayerCategory.main.label("category")
        ).join(LayerCategory, LayerCategory.id == LayerInfo.category_id, isouter=True)

        df = DBQuery.execute_stmt_as_df(session, stmt)
        return {"payload": df.to_dict(orient='records')}


@dch_router.get("/layer_info/{uuid}/")
async def get_layer_info(uuid: str):
    """
    Fetches detailed metadata for a specific layer using its UUID.
    Uses the managed_session pattern for efficient connection handling.
    """
    with LayerInfo.managed_session() as session:
        # 1. Use modern select syntax
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        info = session.execute(stmt).scalar_one_or_none()

        if info:
            # 2. Build response object
            res = {
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
                "dateRangeURL": info.date_range_url
            }

            return {"payload": res}

    # 3. Return 204 No Content if layer doesn't exist
    return Response(status_code=204)


from sqlalchemy import select
from fastapi import Response


@dch_router.get("/layer_extent/{uuid}/")
async def get_layer_extent(uuid: str):
    """
    Returns the bounding box (extent) in EPSG:3857 for a specific layer.
    """
    with LayerInfo.managed_session() as session:
        # Use modern select syntax
        stmt = select(LayerInfo.extent_3857).where(LayerInfo.uuid == uuid)
        extent = session.execute(stmt).scalar_one_or_none()

        if extent is not None:
            return {"payload": extent}

    # Return 204 if the UUID doesn't exist
    return Response(status_code=204)


@dch_router.get("/raster_tile/{uuid}/{z}/{x}/{y}/", responses={200: {"content": {"image/png": {}}}}, response_class=Response)
@dch_router.get("/raster_tile/{uuid}/{z}/{x}/{y}", responses={200: {"content": {"image/png": {}}}}, response_class=Response)
async def get_layer_tms(uuid: str, z: int, x: int, y: int):
    tile_size = 256

    with LayerInfo.managed_session() as session:
        try:
            info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()
            if info is None or (info.data_model and info.data_model.code) != DataModel.R.name:
                raise HTTPException(status_code=404, detail="Layer not found or not raster")

            params = info.layer_accessibility.params or {}
            file_path = params.get("file_path")
            if not file_path:
                raise HTTPException(status_code=404, detail="Raster path missing in params")

            # normalize boolean param (your code mixes 'true' string)
            is_media_rel = str(params.get("is_path_media_rel", "")).lower() in ("true", "1", "yes")

            # local dev shortcut for s3://
            if file_path.startswith("s3://"):
                file_path = file_path[len("s3://"):]
                is_media_rel = True

            file_path = os.path.join(MEDIA_DIR, file_path) if is_media_rel else file_path

            cog_raster = COGRaster.open_from_local(file_path)
            cm = COGRaster.create_color_map(info.layer_styling) if info.layer_styling else None

            data = cog_raster.read_tile_as_png(x, y, z, color_map=cm, tile_size=tile_size)
            res = data.getvalue()
            return Response(content=res, media_type="image/png")

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(f"[TileError] uuid={uuid}, z={z}, x={x}, y={y} — {e}")
            empty_tile = COGRaster.create_empty_image(tile_size, tile_size)
            return Response(content=empty_tile.getvalue(), media_type="image/png")


from fastapi import Query
from sqlalchemy.orm import Session

@dch_router.get("/wfs/{uuid}/{format}/")
async def get_wfs(
    uuid: str,
    format: str,
    limit: int = Query(20000, ge=1, le=200000),
    offset: int = Query(0, ge=0),
    cols: Optional[List[str]] = Query(None),  # ?cols=a&cols=b
):
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

    # ---- column whitelist (optional) ----
    allowed = set(access.cols_name or [])
    # always allow geom col (even if not present in cols_name list)
    requested = cols or list(allowed)

    bad = [c for c in requested if c not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

    safe_cols = ", ".join(DBQuery.safe_ident(c) for c in requested)
    select_list = f"{safe_cols}, {geom_col}" if safe_cols else geom_col

    # stable pagination if pk exists
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
            geom_col=access.geometry_col,  # raw for DBQuery internals
            crs=f"EPSG:{info.srid}",
        )

    if gdf.empty:
        return Response(status_code=204)

    geojson = gdf.to_crs(epsg=4326).to_json(default=handle_timestamp)
    return {
        "payload": DCHUtils.gzip_data(geojson),
        "meta": {"format": "geojson", "limit": limit, "offset": offset, "count": len(gdf)},
    }

# Define handle_timestamp globally
def handle_timestamp(obj):
    if isinstance(obj, pd.Timestamp):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    raise TypeError(f'Object of type {type(obj)} is not JSON serializable')



_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")

def cache_table_name(uuid: str) -> str:
    # accept standard UUIDs, then normalize
    if not _UUID_TOKEN_RE.fullmatch(uuid):
        raise HTTPException(status_code=400, detail="Invalid layer uuid")
    token = uuid.replace("-", "")  # 32 hex chars
    return f"main.{DBQuery.safe_ident(f'{token}_xyz')}"

@dch_router.get("/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}")
async def get_layer_mvt(
    uuid: str,
    tile_size: int,
    z: int,
    x: int,
    y: int,
    resolution: float = 0,
    cols: Optional[List[str]] = None
):
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        raise HTTPException(status_code=404)

    access = info.layer_accessibility
    if not access:
        return Response(status_code=204)

    cache_engine = DBBase.get_engine(app_label="cache_db")
    xyz_table = cache_table_name(uuid)

    # 1) cache hit
    with Session(cache_engine) as cache_session:
        cached = cache_session.execute(
            text(f"SELECT mvt FROM {xyz_table} WHERE x=:x AND y=:y AND z=:z"),
            {"x": x, "y": y, "z": z},
        ).scalar()
        if cached:
            return Response(content=bytes(cached), media_type="application/vnd.mapbox-vector-tile")

    # 2) cache miss -> generate
    try:
        external_engine = access.get_external_engine()
        if not external_engine:
            return Response(status_code=204)

        allowed_cols = set(access.cols_name or [])
        requested_cols = cols or list(allowed_cols)

        # enforce whitelist
        bad = [c for c in requested_cols if c not in allowed_cols]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

        # safe idents
        select_cols = ", ".join(DBQuery.safe_ident(c) for c in requested_cols)
        geom_col = DBQuery.safe_ident(access.geometry_col)
        table = DBQuery.safe_ident(access.table_name)

        stmt = f"SELECT {select_cols}, {geom_col} FROM {table}"

        with Session(external_engine) as ext_session:
            mvt_bytes = DBQuery.execute_query_as_mvt(
                session=ext_session,
                stmt=stmt,
                z=z, x=x, y=y,
                geom_col=access.geometry_col,          # function needs raw col name
                layer_name=info.layer_name or "layer",
                extent=tile_size,
                resolution=resolution
            )

        if mvt_bytes:
            with Session(cache_engine) as cache_session:
                cache_session.execute(
                    text(f"INSERT INTO {xyz_table} (x, y, z, mvt) VALUES (:x, :y, :z, :mvt)"),
                    {"x": x, "y": y, "z": z, "mvt": mvt_bytes},
                )
                cache_session.commit()

        return Response(content=mvt_bytes or b"", media_type="application/vnd.mapbox-vector-tile")

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"MVT Failure: {e}")
        return Response(status_code=500)


@dch_router.delete("/clear_cache/{uuid}/")
async def clear_cache(uuid: str):
    try:
        cache_engine = DBBase.get_engine(app_label="cache_db")
    except KeyError:
        raise HTTPException(status_code=500, detail="Cache database configuration missing")

    xyz_table = cache_table_name(uuid)  # uses safe_ident

    with Session(cache_engine) as session:
        try:
            session.execute(text(f"DROP TABLE IF EXISTS {xyz_table}"))
            session.commit()
            return {"payload": f"Cache table {xyz_table} successfully cleared"}
        except Exception as e:
            session.rollback()
            da_logger.error(f"Failed to clear cache for {uuid}: {e}")
            return {"payload": "error clearing cache", "detail": str(e)}

@dch_router.post("/upload_shp_file/")
async def upload_shp_file(
        request: Request,
        current_user: UserInfo = Depends(AuthUtils.get_current_user)
):
    """
    Uploads Shapefile components, saves them to media,
    and imports the data specifically into the 'data' PostGIS database.
    """
    form = await request.form()
    layer_category_id = form.get("layer_category_id")

    # Filter only actual upload files from the form
    upload_files = [v for k, v in form.items() if hasattr(v, "filename")]

    if not upload_files:
        raise HTTPException(status_code=400, detail="No files provided")

    filename_without_ext = None
    file_dir = None

    # 1. Save uploaded files to a unique directory
    for file in upload_files:
        filename = file.filename
        if file_dir is None:
            filename_without_ext = os.path.splitext(filename)[0]
            file_dir = os.path.join(MEDIA_DIR, "uploads", filename_without_ext)
            os.makedirs(file_dir, exist_ok=True)

        file_path = os.path.join(file_dir, filename)
        with open(file_path, 'wb') as f:
            shutil.copyfileobj(file.file, f)

    # 2. Load Shapefile into GeoDataFrame
    shp_fp = os.path.join(file_dir, f"{filename_without_ext}.shp")
    if not os.path.exists(shp_fp):
        raise HTTPException(status_code=400, detail="Missing .shp file in upload")

    try:
        gdf = gpd.read_file(shp_fp)
        gdf.rename_geometry('geom', inplace=True)
        gdf.columns = gdf.columns.str.lower()

        # Drop standard ID columns to avoid conflicts with DB serials
        cols_to_drop = [c for c in ["objectid", "fid", "id"] if c in gdf.columns]
        if cols_to_drop:
            gdf.drop(columns=cols_to_drop, inplace=True)

        # 3. Import into the 'data' Database
        uuid = DCHUtils.get_uuid1()
        table_name = DCHUtils.short_table_name(filename_without_ext)

        # UPDATED: Using DATA_DB_KEY instead of DEFAULT_DB_KEY
        engine = DBBase.get_engine(DATA_DB_KEY)

        # to_postgis handles the table creation and data insertion
        # Ensure the 'data' DB has PostGIS extension enabled
        gdf.to_postgis(table_name, con=engine, if_exists='replace', index=False)

        # 4. Create Metadata (LayerInfo & LayerAccessibility)
        # UPDATED: Informing the utility that the layer resides in DATA_DB_KEY
        # engine = DBBase.get_engine(DATA_DB_KEY)
        DCHUtils.create_vector_layer_info(
            engine,
            table_name,
            uuid,
            layer_category_id=layer_category_id
        )

        return {
            "payload": {
                "msg": "Shapefile uploaded and imported to DATA database successfully",
                "uuid": uuid,
                "table": table_name,
                "database": DATA_DB_KEY
            }
        }

    except Exception as e:
        da_logger.exception(f"PostGIS Import Error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to import spatial data to {DATA_DB_KEY} database"
        )


@dch_router.get('/get_style/{uuid}/{map_uuid}/')
def get_style(uuid: str, map_uuid: str):
    """
    Retrieves style from map override or falls back to global default.
    """
    style = None
    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            stmt = select(MapInfo).where(MapInfo.uuid == map_uuid)
            map_obj = session.execute(stmt).scalar_one_or_none()
            if map_obj:
                layer_entry = next((l for l in map_obj.layers if l.get("uuid") == uuid), None)
                if layer_entry:
                    style = layer_entry.get("style")

    if style is None:
        layer_info = LayerInfo.get_layer_info_by_uuid(uuid)
        if layer_info:
            style = layer_info.layer_styling

    return {"payload": style} if style else Response(status_code=204)


@dch_router.post('/save_style/{uuid}/{map_uuid}/')
async def save_style(request: Request, uuid: str, map_uuid: str):
    """
    Saves styling either to a specific map context or globally to the layer.
    """
    layer_styling = await request.json()

    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            stmt = select(MapInfo).where(MapInfo.uuid == map_uuid)
            obj = session.execute(stmt).scalar_one_or_none()
            if obj:
                # Update specific layer style in the layers JSON list
                new_layers = []
                for l in (obj.layers or []):
                    if l.get("uuid") == uuid:
                        l["style"] = layer_styling
                    new_layers.append(l)
                obj.layers = new_layers
                obj.save(session=session, allow_update=True)
                return {"payload": "Map Layer style saved successfully"}
    else:
        with LayerInfo.managed_session() as session:
            stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
            obj = session.execute(stmt).scalar_one_or_none()
            if obj:
                obj.layer_styling = layer_styling
                obj.save(session=session, allow_update=True)
                return {"payload": "Global Layer style saved successfully"}

    return Response(status_code=204)


@dch_router.post("/upload_sld_style/{uuid}/")
async def save_sld(uuid: str, request: Request):
    """
    Uploads SLD or JSON styles and parses raster symbolizers if necessary.
    """
    form = await request.form()
    file = form.get("file")
    if not file: raise HTTPException(status_code=400, detail="No file uploaded")

    content = await file.read()

    with LayerInfo.managed_session() as session:
        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()
        if not obj: raise HTTPException(status_code=404)

        if "json" in file.content_type:
            obj.layer_styling = json.loads(content)
        else:
            sld_str = content.decode("utf-8")
            if obj.data_model.code == DataModel.R.name:
                # Raster Logic
                raster = obj.to_raster()
                summary = BandProcess.get_summary_data(raster.get_data_array(1), nodata=raster.get_nodata_value())
                obj.layer_styling = DCHUtils.raster_symbolizer_parser(sld_str, summary,
                                                                      'float' in raster.get_profile()['dtype'])
            else:
                obj.layer_styling = {"type": "sld", "style": sld_str}

        obj.save(session=session, allow_update=True)
    return {"payload": "Style saved successfully"}


@dch_router.get("/download_da_style/{uuid}/")
async def download_da_style(uuid: str):
    """
    Downloads the styling configuration for a layer as either an .sld or .json file.
    Uses managed_session for proper resource handling.
    """
    with LayerInfo.managed_session() as session:
        # 1. Modern selection logic
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        obj = session.execute(stmt).scalar_one_or_none()

        if obj is not None and obj.layer_styling:
            layer_style = obj.layer_styling
            filename_base = obj.layer_name or f"style_{uuid}"

            # 2. Check if the style is an SLD (XML) format
            if isinstance(layer_style, dict) and layer_style.get("type") == "sld":
                content = layer_style.get("style", "")
                media_type = "text/xml; charset=utf-8"
                extension = "sld"
            else:
                # 3. Fallback to standard JSON format
                content = json.dumps(layer_style, indent=2)
                media_type = "application/json; charset=utf-8"
                extension = "json"

            # 4. Construct Response
            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename_base}.{extension}"',
                    "Content-Length": str(len(content.encode("utf-8")))
                }
            )

    # 5. Return 204 No Content if layer or styling is missing
    return Response(status_code=204)


@dch_router.get("/layer_fields/{uuid}/")
async def get_layer_fields(uuid: str):
    """
    Retrieves the field/column names for a specific layer.
    Uses the managed_session pattern for automatic connection handling.
    """
    with LayerInfo.managed_session() as session:
        # 1. Use modern select syntax targeting the specific UUID
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        obj = session.execute(stmt).scalar_one_or_none()

        # 2. Extract columns from accessibility params
        if obj is not None and obj.layer_accessibility:
            params = obj.layer_accessibility.params
            columns = params.get('columns', [])
            return {"payload": columns}

    # 3. Return 204 No Content if layer or column metadata is missing
    return Response(status_code=204)


@dch_router.get("/layer_field_distinct_values/{uuid}/{field_name}/{field_type}/")
async def get_field_distinct_values(
        uuid: str,
        field_name: str,
        field_type: str,
        classification: str = "Stats",
        no_of_classes: int = 0
):
    """
    Retrieves unique values or statistical breaks for a specific layer field.
    Supports Quantile, Equal Interval, and Natural Breaks (Jenks).
    """
    # 1. Fetch metadata using managed session
    with LayerInfo.managed_session() as session:
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        obj = session.execute(stmt).scalar_one_or_none()

    if obj is None:
        return Response(status_code=204)

    # 2. Fetch data from the actual layer source (External DB or local)
    df = obj.to_layer_df(cols=[field_name])

    if df.empty or field_name not in df.columns:
        return {"payload": []}

    res = []
    # 3. Process Numeric Classification
    if field_type == "number" and no_of_classes > 0:
        data_series = df[field_name].dropna()
        if data_series.empty:
            return {"payload": []}

        if classification.lower() == "quantile":
            #
            # qcut divides data into bins of equal size (frequency)
            bins = pd.qcut(data_series, q=int(no_of_classes), duplicates='drop')
            res = sorted([interval.right for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "EqualInterval":
            #
            # cut divides data into bins of equal range (width)
            bins = pd.cut(data_series, bins=int(no_of_classes))
            res = sorted([interval.right for interval in bins.unique()])
            res.insert(0, float(data_series.min()))

        elif classification == "NaturalBreak":
            #
            # Minimizes variance within classes and maximizes variance between classes
            res = jenkspy.jenks_breaks(data_series.tolist(), n_classes=int(no_of_classes))

        else:
            # Default to distinct numeric values
            res = data_series.unique().tolist()
    else:
        # 4. Handle Categorical/String values
        res = df[field_name].unique().tolist()

    return {"payload": res}




@dch_router.get(
    "/layer_attributes/{uuid}/",
    response_model=Union[AttributeTableResponse, dict],
    summary="Get Layer Attributes"
)
def get_layer_attributes(uuid: str):
    """
    Returns the attribute table for Vector layers or a class/band breakdown
    for Raster layers.
    """
    # 1. Fetch metadata using managed session
    with LayerInfo.managed_session() as session:
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        info = session.execute(stmt).scalar_one_or_none()

    if info is None:
        return Response(status_code=204)

    # 2. Case: Vector Layer (V)
    # Fetches attributes from the underlying database table
    if info.data_model.code == DataModel.V.name:
        df = info.to_layer_df()
        if not df.empty:
            return ManageDataFrameUtils.grid_response(
                df,
                pkCols=info.layer_accessibility.pk_cols
            )

    # 3. Case: Raster Layer (R)
    elif info.data_model.code == DataModel.R.name:
        raster = info.to_raster()
        spec_res = raster.get_spectral_resolution()

        # Breakdown of single-band thematic rasters (e.g., Landcover)
        if spec_res == 1 and info.layer_styling:
            style = info.layer_styling
            # Get values from styling palette or explicit values list
            if 'values' in style:
                values = style['values']
            else:
                palette = style.get('palette', {})
                values = [int(key) for key in palette.keys()]

            data = raster.get_data_array(1)
            labels = style.get('labels', [])

            # Calculate Spatial Resolution in km for area reporting
            # Ground Sampling Distance (GSD) conversion
            spatial_res = raster.get_spatial_resolution(in_meter=True)
            res_in_km = (spatial_res[0] / 1000, spatial_res[1] / 1000)

            # Perform the pixel counting and area calculation
            res = BandProcess.get_value_area_data(
                data,
                raster.get_nodata_value(),
                res_in_km,
                values,
                labels,
                "sq. km."
            )
            df = pd.DataFrame(res).rename(columns={'area': 'area_sq_km'})
        else:
            # Multi-band rasters: return standard statistical summaries
            df = raster.get_band_summaries()

        return ManageDataFrameUtils.grid_response(df, pkCols=[])

    return Response(content='{"payload": {"msg": "No attribute found"}}', status_code=204)





@dch_router.get("/get_pixel_value/{uuid}/{long}/{lat}/")
async def get_pixel_value(uuid: str, long: float, lat: float):
    """
    Retrieves the specific pixel value (cell value) at a given longitude and latitude.
    Optimized for COG (Cloud Optimized GeoTIFF) access.
    """
    # 1. Fetch metadata using managed session
    with LayerInfo.managed_session() as session:
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        info = session.execute(stmt).scalar_one_or_none()

    if not info:
        return Response(status_code=204)

    # 2. Check if the layer is a Raster (R)
    # Using .code for Enum compatibility
    if info.data_model.code == DataModel.R.name:
        try:
            # info.to_cog() handles path resolution (MEDIA_DIR/COG_DIR)
            cog_raster = info.to_cog()

            if cog_raster:
                #
                # Performs the coordinate transformation and pixel extraction
                data = cog_raster.get_pixel_value_at_long_lat(long, lat)
                return {"payload": data}
            else:
                da_logger.error(f"Raster file not found for UUID: {uuid}")

        except Exception as e:
            da_logger.exception(f"Error extracting pixel value: {e}")
            raise HTTPException(status_code=500, detail="Internal processing error")

    return Response(status_code=204)


@dch_router.get("/get_feature_detail/{uuid}/{col_name}/{col_val}/")
async def get_feature_detail(uuid: str, col_name: str, col_val: str):
    """
    Retrieves the attribute details for a single feature based on a column name and value.
    Excludes the geometry column to keep the response lightweight for information popups.
    """
    # 1. Fetch metadata using managed session
    with LayerInfo.managed_session() as session:
        stmt = select(LayerInfo).where(LayerInfo.uuid == uuid)
        info = session.execute(stmt).scalar_one_or_none()

    if info is None:
        return Response(status_code=204)

    # 2. Fetch specific row detail
    # info.get_row_detail handles the connection to the external PostGIS engine if necessary
    rs = info.get_row_detail(col_name, col_val)

    if rs and len(rs) > 0:
        # 3. Clean the result: Convert the first record to a dict and remove 'geom'
        # rs[0] is typically a Row or dict-like object
        row_dict = dict(rs[0])

        # Remove geometry column to avoid sending heavy WKB/GeoJSON strings
        row_dict.pop('geom', None)
        row_dict.pop('geometry', None)  # Safety check for alternative naming

        return {"payload": row_dict}

    return Response(status_code=204)



@dch_router.get("/get_feature_geometry/{uuid}/{pk_values}/")
async def get_feature_geometry(uuid: str, pk_values: str):
    with LayerInfo.managed_session() as session:
        info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        return Response(status_code=204)

    pk_cols = info.layer_accessibility.pk_cols or []
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

    # Fetch geometry via a safe query (GeoJSON or WKT — use your existing get_geometry if you refactor it)
    access = info.layer_accessibility
    engine = access.get_external_engine()
    if not engine:
        return Response(status_code=204)

    geom_col = DBQuery.safe_ident(access.geometry_col)
    table = DBQuery.safe_ident(access.table_name)

    # WKT in 3857 (matches your earlier design)
    q = text(f"SELECT ST_AsText(ST_Transform({geom_col}, 3857)) AS geom FROM {table} WHERE {where_sql} LIMIT 1")
    with Session(engine) as s:
        geom = s.execute(q, params).scalar_one_or_none()

    return {"payload": geom} if geom else Response(status_code=204)


@dch_router.get("/column_value/{uuid}/{pk_val}/{col_name}/")
async def get_column_value(uuid: str, pk_val: str, col_name: str):
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
    with Session(engine) as s:
        val = s.execute(q, params).scalar_one_or_none()

    return {"payload": {col_name: val if val is not None else ""}}

@dch_router.get("/get_db_connection/")
async def get_db_connection():
    """
    Modern fetch using managed session and DBQuery.
    """
    with DBConnection.managed_session() as session:
        stmt = select(DBConnection.id, DBConnection.name)
        df = DBQuery.execute_stmt_as_df(session, stmt)
        return {"payload": df.to_dict(orient="records")}


@dch_router.get("/layer_categories/")
def layer_categories():
    """
    Fetches categories using managed session.
    """
    with LayerCategory.managed_session() as session:
        stmt = select(LayerCategory)
        qs = session.execute(stmt).scalars().all()
        cat = [{"pk": obj.id, "name": f"{obj.main}-{obj.sub}"} for obj in qs]
    return {"payload": cat}


@dch_router.post("/add_model_row/")
async def add_model_row(request: Request):
    form = await request.form()
    model_name = form.get("model_name")
    row = {}
    for key in form.keys():
        if key != "model_name":
            row[key] = form.get(key)
    obj = ModelUtils.add_model_obj(model_name, row)
    if obj is not None:
        return {"payload": {"msg": "row added"}}
    return Response(status_code=400)


@dch_router.get("/add_layer_url_info/{layer_title}/{layer_category_id}/{layer_url}/{url_type}/")
async def add_layer_url_info(layer_title, layer_category_id: str, layer_url: str, url_type: str):
    lf = LayerInfo()
    lf.uuid = DCHUtils.get_uuid1()
    lf.title = layer_title
    lf.layer_name = layer_title.replace(" ", "_")
    lf.data_model = DataModel.URL.name
    lf.category = layer_category_id
    lf.layer_access_id = LayerAccessibility.create_url_accessibility()
    lf.save()
    return {"payload": {"msg": "row added"}}


@dch_router.get("/navigation_geometry/{map_uuid}/{selected_key}/{node_id}/")
async def get_navigation_geometry(map_uuid: str, selected_key: str, node_id: str):
    """
    Fetches the geometry for a navigation node (e.g., an administrative boundary)
    from the database specified in the map's navigation settings.
    """
    # 1. Fetch Map Info using managed_session
    with MapInfo.managed_session() as session:
        stmt = select(MapInfo).where(MapInfo.uuid == map_uuid)
        map_info = session.execute(stmt).scalar_one_or_none()

    if not map_info or not map_info.navigation_setting:
        return Response(status_code=204)

    # 2. Extract settings for the selected navigation key
    nav_setting = map_info.navigation_setting.get(selected_key)
    if not nav_setting:
        return Response(status_code=204)

    try:
        # 3. Get the engine for the database specified in settings (e.g., 'pak')
        target_engine = DBBase.get_engine(nav_setting["db_key"])

        # 4. Construct the spatial query
        # We transform to 4326 for GeoJSON standard compliance
        geom_col = nav_setting['geom_col']
        table = nav_setting['table']
        query = f"""
            SELECT id, ST_Transform("{geom_col}", 4326) as geom 
            FROM {table} 
            WHERE id = :node_id
        """

        # 5. Execute query on the target database using DBQuery
        with Session(target_engine) as target_session:
            # execute_query_as_gdf handles the transformation from WKB to GeoPandas
            gdf = DBQuery.execute_query_as_gdf(
                target_session,
                query,
                params={"node_id": node_id},
                geom_col="geom",
                crs="EPSG:4326"
            )

        if gdf.empty:
            return Response(status_code=204)

        # 6. Convert GeoDataFrame to GeoJSON
        geojson_str = gdf.to_json()
        return {"payload": json.loads(geojson_str)}

    except Exception as e:
        da_logger.error(f"Failed to fetch navigation geometry for node {node_id}: {e}")
        return Response(status_code=500)


@dch_router.get("/navigation_list/{map_uuid}/")
async def get_navigation_list(map_uuid: str):
    """
    Retrieves hierarchical navigation data (e.g., admin boundaries) for a specific map.
    The settings are stored in the MapInfo metadata, but data is fetched from
    external databases defined per-setting.
    """
    # 1. Fetch Map Info using managed_session
    with MapInfo.managed_session() as session:
        stmt = select(MapInfo).where(MapInfo.uuid == map_uuid)
        map_info = session.execute(stmt).scalar_one_or_none()

    if not map_info or not map_info.navigation_setting:
        return Response(status_code=204)

    final_res = {}

    # 2. Iterate through configured navigation sources
    for key, s in map_info.navigation_setting.items():
        try:
            res = {"name": s["name"]}

            # 3. Resolve the engine for the specified external database (e.g., 'pak')
            target_engine = DBBase.get_engine(s["db_key"])

            # 4. Construct query for non-geometry attributes
            # We exclude the geometry column specified in settings
            geom_col = s.get('geom_col', 'geom')
            table_name = s['table']

            # We use a subquery to exclude the geometry column for cleaner performance
            # or simply specify the required fields from your INavigationItem interface
            query = f"""
                SELECT id, name, level, parent, "parentId" 
                FROM {table_name} 
                ORDER BY level, name
            """

            # 5. Execute on the target external database
            with Session(target_engine) as target_session:
                df = DBQuery.execute_stmt_as_df(target_session, query)

            res["data"] = df.to_dict(orient="records")
            final_res[key] = res

        except Exception as e:
            da_logger.error(f"Navigation error for table {s.get('table')}: {e}")
            # We continue to the next setting even if one fails
            continue

    return {"payload": final_res}


@dch_router.post("/send-email/")
async def dch_send_email(email_data: EmailRequest):
    """
    Sends an email via SMTP. Uses background task for better performance.
    """
    try:
        send_email(email_data.recipient, email_data.subject, email_data.body)
        return {"message": f"Email successfully sent to {email_data.recipient}"}
    except HTTPException as e:
        raise e


@dch_router.get("/raster_data_model_types", tags=["Raster Info"])
async def get_data_model_types():
    return [{"key": m.name, "value": m.value} for m in ModelType]


@dch_router.post(
    "/add_raster_layer_info",
    response_model=UploadRasterResponse,
    summary="Upload and Register Raster Layer",
    description="Uploads a raster file, converts to Cloud Optimized GeoTIFF (COG), and registers it into the system."
)
async def upload_raster_layer(
        title: str = Form(...),
        categoryId: int = Form(...),
        temporal: str = Form(...),
        data_model_type: str = Form(...),
        rasterFile: UploadFile = File(...),
        session: Session = Depends(get_sync_session),
):
    """
    Uploads a raster file, converts it to COG if needed,
    stores it under MEDIA_DIR/APP_NAME/category/main/sub,
    and registers metadata in LayerInfo and LayerAccessibility.
    """

    # Step 1: Validate category
    category = session.get(LayerCategory, categoryId)
    if not category:
        raise HTTPException(status_code=400, detail="Invalid category ID")

    # Step 2: Build destination path
    folder_path = os.path.join(MEDIA_DIR, APP_NAME, category.main, category.sub or "general")
    os.makedirs(folder_path, exist_ok=True)

    # Step 3: Generate UUID and file path
    uuid_str = DCHUtils.get_uuid1()
    file_ext = os.path.splitext(rasterFile.filename)[1]
    unique_name = f"{title}_{uuid_str}_{temporal}{file_ext}"
    raster_path = os.path.join(folder_path, unique_name)

    # Step 4: Save original file
    with open(raster_path, "wb") as f:
        shutil.copyfileobj(rasterFile.file, f)

    # Step 5: Convert to COG if not already
    try:
        _ = COGRaster.open_from_local(raster_path)  # already COG
        cog_path = raster_path
    except Exception:
        # Convert to COG with suffix "_cog"
        cog_path = raster_path.replace(file_ext, f"_cog{file_ext}")
        cog_path = COGRaster.create_cog(raster_path, des_path=cog_path)

        # Delete original raster
        try:
            os.remove(raster_path)
        except Exception as e:
            da_logger.warning(f"Warning: Failed to delete original file: {e}")

        raster_path = cog_path

    raster = RioRaster(cog_path)
    # Step 6: Create LayerAccessibility
    relative_path = os.path.relpath(cog_path, MEDIA_DIR)
    rad_res = raster.get_radiometric_resolution()
    access = LayerAccessibility(
        accessibility_type=AccessibilityType.File.name,
        params={"file_path": relative_path, "is_path_media_rel": True,
                "spectral_resolution": raster.get_spectral_resolution(),
                "spatial_resolution": raster.get_spatial_resolution(),
                "radiometric_resolution": f'{rad_res}'}
    )
    session.add(access)
    session.commit()
    session.refresh(access)

    # Step 7: Convert temporal to date
    try:
        temporal_date = datetime.strptime(temporal, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid temporal format. Use YYYY-MM-DD.")

    # Step 8: Create LayerInfo
    file_name = os.path.basename(cog_path)
    layer_name = os.path.splitext(file_name)[0]
    envelop = raster.get_envelop()

    layer_info = LayerInfo(
        uuid=uuid_str,
        app_label="dch",
        title=title,
        layer_name=layer_name,
        srid=raster.get_raster_srid(),
        extent=envelop.total_bounds.tolist(),
        extent_3857=envelop.to_crs(epsg=3857).total_bounds.tolist(),
        data_model="R",
        model_type=data_model_type,
        geom_type=[],
        uploaded_by=1,  # Replace with actual user later
        temporal_res=temporal_date,
        category=category.id,
        layer_access_id=access.id,
        quality_level=None,
        format="COG",
    )

    session.add(layer_info)
    session.commit()
    session.refresh(layer_info)

    return {
        "uuid": layer_info.uuid,
        "msg": "Raster uploaded and LayerInfo created successfully.",
        "path": raster_path,
    }


@dch_router.get("/get_legend_graphic/{uuid}", response_class=Response)
@dch_router.get("/get_legend_graphic/{uuid}/", response_class=Response)
def get_legend_graphic(uuid: str, session: Session = Depends(get_sync_session)):
    try:
        # Step 1: Fetch layer info from DB
        info = session.query(LayerInfo).filter_by(uuid=uuid).first()
        if not info or not info.layer_styling:
            raise HTTPException(status_code=404, detail="Layer style not found.")

        style = info.layer_styling
        cp = COGRaster.create_color_map(style)

        # Step 2: Determine labels
        if "labels" in style:
            labels = style["labels"]
        elif isinstance(cp, dict):
            labels = [str(k) for k in cp.keys()]
        elif isinstance(cp, list):
            labels = [str(round(v[0][0], 2)) for v in cp]
            # Add the last edge for completeness
            if cp and cp[-1][0]:
                labels.append(str(round(cp[-1][0][-1], 2)))
        else:
            labels = []

        if not labels:
            raise HTTPException(status_code=400, detail="No labels available for legend.")

        # Step 3: Setup ticks (should match label count)
        ticks = list(range(len(labels)))
        if len(ticks) != len(labels):
            raise HTTPException(status_code=400, detail="Mismatch between ticks and labels.")

        # Step 4: Prepare figure
        is_horizontal = False
        fig: Figure = plt.figure(figsize=[500 / 96, 1] if is_horizontal else [500 / 96, 0.4 * len(labels)])
        ax: Axes = fig.add_axes([0.05, 0.8, 0.9, 0.8] if is_horizontal else [0.1, 0.15, 0.05, 0.8])

        # Step 5: Parse color palette
        palette = style.get("palette")
        if not palette:
            raise HTTPException(status_code=400, detail="Palette is missing.")

        if isinstance(palette, dict):
            palette = list(palette.values())

        if not isinstance(palette, list) or len(palette) < 2:
            raise HTTPException(status_code=400, detail="Invalid color palette.")

        # Step 6: Create colormap and colorbar
        cmap = LinearSegmentedColormap.from_list("cdict", palette)

        cbar = colorbar.ColorbarBase(
            ax,
            orientation="horizontal" if is_horizontal else "vertical",
            cmap=cmap,
            norm=plt.Normalize(vmin=ticks[0], vmax=ticks[-1])
        )

        # Step 7: Assign labels to ticks
        if is_horizontal:
            cbar.ax.set_xticks(ticks)
            cbar.ax.set_xticklabels(labels)
        else:
            ax.tick_params(axis="y", which="major", labelsize=10)
            cbar.ax.set_yticks(ticks)
            cbar.ax.set_yticklabels(labels)

        # Step 8: Return the image
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return Response(content=buf.getvalue(), media_type="image/png",
                        headers={"Cross-Origin-Resource-Policy": "cross-origin"})

    except HTTPException:
        raise  # propagate HTTP errors properly
    except Exception as e:
        print(f"[ERROR] Failed to generate legend: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@dch_router.get("/get_raster_info/{uuid}/")
def get_raster_info(uuid: str):
    """
    Retrieves spectral resolution and band summaries for a raster layer.
    Uses the RioRaster and BandProcess utilities.
    """
    try:
        # 1. Fetch Metadata using our clean helper
        info = LayerInfo.get_layer_info_by_uuid(uuid)

        if not info:
            raise HTTPException(status_code=404, detail="Layer metadata not found")

        # 2. Validate Data Model and Accessibility
        if (info.data_model and info.data_model.code) != DataModel.R.name:
            raise HTTPException(status_code=400, detail="Requested layer is not a Raster model")

        access = info.layer_accessibility
        if access.accessibility_type.code == AccessibilityType.File.name:
            # Using the .file_path property we built that handles MEDIA_DIR/COG_DIR
            fp = access.file_path

            if not os.path.exists(fp):
                da_logger.error(f"Raster file missing at: {fp}")
                return Response(status_code=204)

            # 3. Open Raster and extract Band info
            raster = RioRaster(fp)
            band_count = raster.get_spectral_resolution()
            bands_info = {}

            for b in range(1, band_count + 1):
                # RioRaster uses 1-based indexing for bands
                data_array = raster.get_data_array(b)
                nodata = raster.get_nodata_value()

                # Extract statistics (min, max, mean, std, etc.)
                bands_info[b] = BandProcess.get_summary_data(data_array, nodata)

            res = {
                "bandCount": band_count,
                "bandsInfo": bands_info,
                "driver": raster.get_driver_name(),  # Optional extra info
                "crs": str(raster.get_crs())
            }

            return JSONResponse(content={"payload": res})

    except Exception as e:
        da_logger.exception(f"Error extracting raster info for uuid {uuid}: {e}")
        return Response(status_code=500)

    return Response(status_code=204)


@dch_router.post("/save_layer_info/")
async def save_layer_info(request: Request):
    """
    Universal metadata editor with dynamic type casting.
    """
    data = await request.json()
    pk_col = data.get("pk", {}).get("colName")
    pk_val = data.get("pk", {}).get("colValue")
    row_data = data.get("rowData")

    with LayerInfo.managed_session() as session:
        obj = session.execute(select(LayerInfo).where(getattr(LayerInfo, pk_col) == pk_val)).scalar_one_or_none()
        if not obj: raise HTTPException(status_code=404)

        for key, value in row_data.items():
            if hasattr(obj, key):
                # Handle Enums and complex types
                if key == 'data_model':
                    value = DataModel.get_name(value)
                setattr(obj, key, value)

        obj.save(session=session, allow_update=True)
    return {"status": "success", "updated": row_data}
