

import base64
import io
import json
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote_plus
from typing import Any

import geopandas as gpd
from fastapi import APIRouter, Depends, Request, Body, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pyproj import Transformer
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified



from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.api_enum import ModelType, ApplicationURLType
from app.damapql.dch.dtypes import DBConnectionCreate
from app.damapql.dch.models import (
    AccessibilityType, DBConnection, DBQuery, DataModel, LayerAccessibility, LayerInfo,
    LayerCategory, MapInfo, LayerQualityLevel
)
from app.damapql.dch.routers.admin_dependencies import require_dch_admin
from app.damapql.dch.schemas.geojson import GeoJSON
from app.damapql.dch.services.layer_query_services import LayerQueryService
from app.damapql.dch.services.upload_package_service import DCHUploadPackageService
from app.damapql.dch.services.upload_session_service import DCHUploadSessionService
from app.damapql.dch.services.vector_append_service import DCHVectorAppendService
from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils

from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.manage import reconcile_layerinfo, create_layerinfo_from_app_models
from app.damapql.settings import DCH_MEDIA_DIR, DCH_DATA_DB_KEY
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.deps import get_sync_session
from app.damapql.utils.adapters.model_utils import ModelUtils
from app.damapql.utils.crypto import CryptoUtils
from digitalarzengine.io.managers.db_manager import GeoDBManager
from digitalarzengine.utils.singletons import da_logger

# ---------------------------------------------------------------------
# Router Setup
# ---------------------------------------------------------------------
dch_admin_router = APIRouter(prefix="/admin", tags=["dch admin"])
# ---------------------------------------------------------------------
# Shared append/upload service
# This service is used by:
#   1. upload_shp_file        -> create a NEW table
#   2. preview_append_shp     -> inspect append compatibility
#   3. commit_append_shp      -> append into EXISTING table
# ---------------------------------------------------------------------
append_service = DCHVectorAppendService(media_dir=DCH_MEDIA_DIR, logger=da_logger, )



# Columns that cannot be directly mutated via dynamic admin updates
IMMUTABLE_COLS = {
    "id", "uuid", "uploaded_date", "layer_accessibility",
    "layer_category", "quality_level", "layer_access_id"
}

LIST_FIELDS = {"zoom_range", "extent", "extent_3857", "geom_type"}
JSON_FIELDS = {"layer_styling", "layer_setting", "extra_metadata"}


# ---------------------------------------------------------------------
# DB Connection Endpoints
# ---------------------------------------------------------------------
@dch_admin_router.get("/get_db_connection/")
async def get_db_connection(current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Return available DB connections as a small list of id + name.

    Protected endpoint.
    """
    require_dch_admin(current_user)
    with DBConnection.managed_session() as session:
        stmt = select(DBConnection.id, DBConnection.name)
        df = DBQuery.execute_stmt_as_df(session, stmt)
        return {"payload": df.to_dict(orient="records")}


@dch_admin_router.post("/test-db-connection/")
def test_db_connection(payload: DBConnectionCreate, current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Test an external database connection using the submitted credentials.

    Protected endpoint.
    """
    require_dch_admin(current_user)
    try:
        p = payload.params

        user = quote_plus(p.user)
        password = quote_plus(p.password)
        host = p.host
        port = p.port
        dbname = quote_plus(p.name)

        url = f"{payload.engine}://{user}:{password}@{host}:{port}/{dbname}"
        engine = create_engine(url)

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        return {"payload": {"success": True, "msg": "Connection successful"}}

    except Exception as e:
        da_logger.exception(f"DB connection test failed: {e}")
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")


@dch_admin_router.post("/add-db-connection/")
def add_db_connection(payload: DBConnectionCreate, current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Save an external DB connection definition in the system.

    Password is encrypted before storing.

    Protected endpoint.
    """
    require_dch_admin(current_user)

    try:
        p = payload.params
        encrypted_password = CryptoUtils().encrypt_txt(p.password)

        db_conn = DBConnection.get_db_connection(con_name=payload.connection_name, engine=payload.engine, host=p.host,
            port=p.port, user=p.user, password=encrypted_password, db_name=p.name, )

        return {"payload": {"success": True, "msg": f"Connection '{db_conn.name}' added successfully",
            "data": {"id": db_conn.id, "name": db_conn.name}, }}

    except Exception as e:
        da_logger.exception(f"Failed to add DB connection: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@dch_admin_router.get("/db_table_list/{db_id}/")
async def db_table_list(db_id: str, db=Depends(get_sync_session),
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Return the list of spatial tables from a configured external DB connection.

    Protected endpoint.
    """
    require_dch_admin(current_user)

    # obj: DBConnection = DBConnection.query().filter_by(id=db_id).first()
    qs = select(DBConnection).where(DBConnection.id == db_id)
    result = db.execute(qs)
    obj: DBConnection = result.scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="DB connection not found")

    try:
        engine = obj.get_sqlalchemy_engine()
        db_manager = GeoDBManager(engine)
        table_names = db_manager.get_spatial_table_names()

        da_logger.debug(f"Spatial table names for db_id={db_id}: {table_names}")
        return {"payload": table_names}
    except Exception as e:
        da_logger.exception(f"Failed to fetch table list: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch spatial table list")


@dch_admin_router.get("/save_db_layer_info/{db_id}/{table_name}/{layer_category_id}/")
async def save_db_layer_info(
    db_id: str,
    table_name: str,
    layer_category_id: int,  # <-- Type hint ensures FastAPI casts and validates as integer
    db=Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_dch_admin(current_user)

    qs = select(DBConnection).where(DBConnection.id == db_id)
    obj: DBConnection = db.execute(qs).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="DB connection not found")

    try:
        engine = obj.get_sqlalchemy_engine()

        is_created = DCHUtils.create_vector_layer_info(
            engine=engine,
            table_name=table_name,
            layer_category_id=layer_category_id,
            user=current_user,
            db_conn_name=obj.name,
        )

        return {"payload": {"msg": "Layer info created" if is_created else "Failed to create Layer Info"}}
    except Exception as e:
        da_logger.exception(f"Failed to save DB layer info: {e}")
        raise HTTPException(status_code=500, detail="Failed to save DB layer info")


# ---------------------------------------------------------------------
# Dynamic Model Row Endpoints
# ---------------------------------------------------------------------
@dch_admin_router.post("/add_model_row/")
async def add_model_row(request: Request, current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Add a row into a dynamic model using form data.

    Expected:
      - model_name
      - remaining form fields become row values

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

    try:
        obj = ModelUtils.add_model_obj(model_name, row)
        if obj is None:
            raise HTTPException(status_code=400, detail="Failed to add model row")
        return {"payload": {"msg": "Row added successfully"}}
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to add model row: {e}")
        raise HTTPException(status_code=500, detail="Failed to add model row")


@dch_admin_router.post("/save_model_data/")
async def save_model_data(request: Request, model_name: str,
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Update a model row dynamically.

    Expected JSON:
    {
      "pk": {
        "colName": "id",
        "colValue": 1
      },
      "rowData": {
        "title": "New title"
      }
    }

    Protected endpoint.
    """
    require_dch_admin(current_user)

    data = await request.json()
    pk = data.get("pk", {})
    pk_col_name = pk.get("colName")
    pk_col_value = pk.get("colValue")
    row_data = data.get("rowData", {})

    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")

    if not pk_col_name:
        raise HTTPException(status_code=400, detail="pk.colName is required")

    if pk_col_value is None:
        raise HTTPException(status_code=400, detail="pk.colValue is required")

    if not isinstance(row_data, dict):
        raise HTTPException(status_code=400, detail="rowData must be an object")

    model_class = ModelUtils.get_model(model_name)
    if model_class is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    if not hasattr(model_class, pk_col_name):
        raise HTTPException(status_code=400, detail=f"Invalid PK column: {pk_col_name}")

    manager = model_class.manager()
    session = manager.get_session()

    try:
        obj = (session.query(model_class).filter(getattr(model_class, pk_col_name) == pk_col_value).first())

        if obj is None:
            raise HTTPException(status_code=404, detail="Row not found")

        for key, value in row_data.items():
            if not hasattr(model_class, key):
                continue

            model_field = getattr(model_class, key)
            column = model_field.property.columns[0]
            col_type_name = type(column.type).__name__

            # ---------------------------------------------------------
            # If the column is a choice field, convert submitted label
            # back to DB value.
            # ---------------------------------------------------------
            if hasattr(column.type, "choices"):
                choice_map = {label: db_value for db_value, label in column.type.choices}
                value = choice_map.get(value, value)

            # ---------------------------------------------------------
            # If frontend sends an object, normalize it:
            #   - use .code when present
            #   - otherwise serialize to JSON if DB column is not JSON
            # ---------------------------------------------------------
            if isinstance(value, dict):
                if "code" in value:
                    value = value["code"]
                elif col_type_name not in ["JSON", "JSONB"]:
                    value = json.dumps(value)

            setattr(obj, key, value)

        obj.save()
        return {"payload": {"status": "success", "pk": pk, "updated": row_data}}
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to save model data: {e}")
        raise HTTPException(status_code=500, detail="Failed to save model data")
    finally:
        manager.terminate_idle_connections(session=session)
        session.close()


# ---------------------------------------------------------------------
# Layer metadata endpoints
# ---------------------------------------------------------------------
@dch_admin_router.post("/add_layer_url_info/")
async def add_layer_url_info(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Create a URL-based or application-based layer metadata entry.

    Expected JSON:
    {
      "layer_title": "Layer 1",
      "layer_category_id": 1,
      "layer_url": "https://....",
      "url_type": "wms"   # wms, wfs, tms, geoserver, arcgis
    }

    Protected endpoint.
    """
    require_dch_admin(current_user)
    data = await request.json()

    layer_title = (data.get("layer_title") or "").strip()
    layer_category_id = data.get("layer_category_id")
    layer_url = (data.get("layer_url") or "").strip()
    url_type = str(data.get("url_type") or "").strip().lower()

    if not layer_title:
        raise HTTPException(status_code=400, detail="layer_title is required")
    if layer_category_id is None:
        raise HTTPException(status_code=400, detail="layer_category_id is required")
    if not layer_url:
        raise HTTPException(status_code=400, detail="layer_url is required")
    if not (layer_url.startswith("http://") or layer_url.startswith("https://")):
        raise HTTPException(status_code=400, detail="layer_url must start with http:// or https://")
    if not url_type:
        raise HTTPException(status_code=400, detail="url_type is required")

    valid_url_types = {"wms", "wfs", "tms"}
    valid_app_types = {e.value for e in ApplicationURLType}
    allowed_types = valid_url_types | valid_app_types

    if url_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid url_type. Allowed values are: {', '.join(sorted(allowed_types))}",
        )

    try:
        with LayerInfo.managed_session() as session:
            is_app = url_type in valid_app_types
            layer_uuid = DCHUtils.get_uuid1()

            # Ensure unique layer_name
            base_slug = "".join(c if c.isalnum() or c == "_" else "_" for c in layer_title.replace(" ", "_")).lower()
            clean_layer_name = f"{base_slug}_{layer_uuid[:8]}"

            accessibility = LayerAccessibility(
                accessibility_type=(
                    AccessibilityType.Application.name if is_app else AccessibilityType.URL.name
                ),
                params={"url": layer_url, "url_type": url_type},
            )
            session.add(accessibility)
            session.flush()

            layer = LayerInfo(
                uuid=layer_uuid,
                title=layer_title,
                layer_name=clean_layer_name,
                data_model=(DataModel.Application.name if is_app else DataModel.URL.name),
                model_type=(ModelType.Application.name if is_app else ModelType.Api.name),
                category_id=int(layer_category_id),
                layer_access_id=accessibility.id,
                uploaded_by=getattr(current_user, "id", None),
            )
            session.add(layer)
            session.commit()

        return {
            "payload": {
                "msg": "Layer URL info added successfully",
                "uuid": layer_uuid,
                "layer_name": clean_layer_name,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to add URL layer info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add layer URL info: {str(e)}")

@dch_admin_router.post("/save_map_info/")
async def save_map_info(request: Request, current_user: UserInfo = Depends(AuthUtils.get_current_user)):
    require_dch_admin(current_user)
    data = await request.json()
    pk_col = data.get("pk", {}).get("colName")
    pk_val = data.get("pk", {}).get("colValue")
    row_data = data.get("rowData")

    if not pk_col or pk_val is None or not isinstance(row_data, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    if not hasattr(MapInfo, pk_col):
        raise HTTPException(status_code=400, detail=f"Invalid LayerInfo PK column: {pk_col}")

    try:
        # normalize known JSON fields
        json_list_fields = {"layers"}
        for field in json_list_fields:
            if field in row_data and isinstance(row_data[field], str):
                try:
                    row_data[field] = json.loads(row_data[field])
                except json.JSONDecodeError:
                    raise HTTPException(status_code=400, detail=f"Invalid JSON for field '{field}'")

        with MapInfo.managed_session() as session:
            obj = session.execute(select(MapInfo).where(getattr(MapInfo, pk_col) == pk_val)).scalar_one_or_none()
            if obj is None:
                raise HTTPException(status_code=404, detail="Map Info not found")

            for key, value in row_data.items():
                if hasattr(obj, key):
                    setattr(obj, key, value)
                    if key in json_list_fields:
                        flag_modified(obj, key)

            obj.save(session=session)

        return {"payload": {"status": "success", "updated": row_data}}
    except HTTPException:
        da_logger.exception(f"Failed to save map info: {pk_val}")
        raise
    except Exception as e:
        da_logger.exception(f"Failed to save map info: {e}")
        raise HTTPException(status_code=500, detail="Failed to save map info")


@dch_admin_router.post("/save_layer_info/")
async def save_layer_info(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Update LayerInfo row fields dynamically.

    Expected JSON:
    {
      "pk": {"colName": "id", "colValue": 1},
      "rowData": {"title": "Updated title", "extent": "[...]"}
    }
    """
    require_dch_admin(current_user)

    data = await request.json()
    pk_info = data.get("pk") or {}
    pk_col = pk_info.get("colName")
    pk_val = pk_info.get("colValue")
    row_data = data.get("rowData")

    if not pk_col or pk_val is None or not isinstance(row_data, dict):
        raise HTTPException(status_code=400, detail="Invalid payload: 'pk' and 'rowData' are required.")

    if not hasattr(LayerInfo, pk_col):
        raise HTTPException(status_code=400, detail=f"Invalid LayerInfo PK column: {pk_col}")

    try:
        with LayerInfo.managed_session() as session:
            stmt = select(LayerInfo).where(getattr(LayerInfo, pk_col) == pk_val)
            obj: LayerInfo = session.execute(stmt).scalar_one_or_none()

            if obj is None:
                raise HTTPException(status_code=404, detail="LayerInfo not found")

            # -------------------------------------------------------------
            # 1. Resolve Category ID from 'main' / 'sub' / combined label
            # -------------------------------------------------------------
            if "category" in row_data and "category_id" not in row_data:
                cat_val = row_data.pop("category")
                if cat_val:
                    cat_str = str(cat_val).strip()
                    # Handle composite "Main - Sub" or just "Main"
                    if " - " in cat_str:
                        main_part, sub_part = [p.strip() for p in cat_str.split(" - ", 1)]
                        cat_record = session.execute(
                            select(LayerCategory).where(
                                LayerCategory.main == main_part,
                                LayerCategory.sub == sub_part,
                            )
                        ).scalar_one_or_none()
                    else:
                        cat_record = session.execute(
                            select(LayerCategory).where(
                                (LayerCategory.main == cat_str) | (LayerCategory.sub == cat_str)
                            )
                        ).scalars().first()

                    if cat_record:
                        row_data["category_id"] = cat_record.id

            extent_updated = False

            # -------------------------------------------------------------
            # 2. Field Normalization and Assignment
            # -------------------------------------------------------------
            for key, value in row_data.items():
                if key in IMMUTABLE_COLS or not hasattr(obj, key):
                    continue

                # ChoiceType / Enum normalization
                if key == "data_model":
                    value = DataModel.get_name(value)
                elif key.lower() == "model_type":
                    value = ModelType.get_name(value)

                # List / Array Fields
                elif key in LIST_FIELDS:
                    if isinstance(value, str):
                        value = value.strip()
                        if not value:
                            value = None
                        else:
                            try:
                                value = json.loads(value)
                            except json.JSONDecodeError:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"Field '{key}' must be a valid JSON array.",
                                )

                    if isinstance(value, (list, tuple)):
                        if key in {"extent", "extent_3857"}:
                            value = [float(v) for v in value]
                            if key == "extent":
                                extent_updated = True
                        elif key == "zoom_range":
                            value = [int(v) for v in value]

                # JSON / Dict Fields
                elif key in JSON_FIELDS and isinstance(value, str):
                    value = value.strip()
                    if not value:
                        value = None
                    else:
                        try:
                            value = json.loads(value)
                        except json.JSONDecodeError:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Field '{key}' must be a valid JSON object.",
                            )

                setattr(obj, key, value)

                if key in LIST_FIELDS or key in JSON_FIELDS:
                    flag_modified(obj, key)

            # -------------------------------------------------------------
            # 3. Synchronize extent_3857 if native extent was changed
            # -------------------------------------------------------------
            if extent_updated and not getattr(obj, "extent_3857", None):
                if hasattr(obj, "recalculate_extent_3857"):
                    obj.recalculate_extent_3857()
                    flag_modified(obj, "extent_3857")

            obj.save(session=session)

        # Clear vector tile cache if visual or spatial parameters changed
        if any(k in row_data for k in {"zoom_range", "layer_styling", "extent", "extent_3857"}):
            try:
                mvt_cache.clear_layer_cache(obj.uuid)
            except Exception as cache_err:
                da_logger.warning(f"Failed to clear tile cache for {obj.uuid}: {cache_err}")

        return {"payload": {"status": "success", "updated": row_data}}

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to save layer info: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save layer info: {str(e)}")


# ---------------------------------------------------------------------
# Shapefile Upload & Append Pipelines
# ---------------------------------------------------------------------
@dch_admin_router.post("/upload_shp_file/")
async def upload_shp_file(request: Request, current_user: dict = Depends(AuthUtils.require_role("app_manager"))):
    """
    Upload shapefile parts, import into DB, register the new vector layer,
    and remove temporary uploaded files after successful import.

    This version uses:
    - DCHUploadSessionService for generic temp upload/session handling
    - DCHUploadPackageService for shapefile package validation/resolution
    """
    form = await request.form()
    layer_category_id = form.get("layer_category_id")

    if not layer_category_id:
        raise HTTPException(status_code=400, detail="layer_category_id is required")

    upload_service = DCHUploadSessionService(media_dir=DCH_MEDIA_DIR, logger=da_logger, )
    package_service = DCHUploadPackageService(upload_service=upload_service)

    upload_id = None
    success = False

    try:
        # ------------------------------------------------------------------
        # Save uploaded files into a generic upload session
        # ------------------------------------------------------------------
        upload_id, session_dir, saved_files = await upload_service.save_files_from_form(form=form,
            upload_type="shapefile", metadata={"purpose": "vector_import"}, )

        # ------------------------------------------------------------------
        # Resolve and validate shapefile package
        # Ensures:
        #   - .shp exists
        #   - .shx exists
        #   - .dbf exists
        #   - required files share the same basename
        # ------------------------------------------------------------------
        shape_info = package_service.resolve_shapefile(upload_id)
        shp_fp = shape_info["main_file"]
        filename_without_ext = shape_info["base_name"]

        # ------------------------------------------------------------------
        # Read shapefile into GeoDataFrame
        # ------------------------------------------------------------------
        gdf = gpd.read_file(shp_fp)
        gdf.rename_geometry("geom", inplace=True)
        gdf.columns = gdf.columns.str.lower()

        # Remove common ID columns if they exist
        cols_to_drop = [c for c in ["objectid", "fid", "id"] if c in gdf.columns]
        if cols_to_drop:
            gdf.drop(columns=cols_to_drop, inplace=True)

        # ------------------------------------------------------------------
        # Prepare table / DB connection
        # ------------------------------------------------------------------
        uuid = DCHUtils.get_uuid1()
        table_name = DCHUtils.short_table_name(filename_without_ext, li_uuid=uuid)
        engine = DBBase.get_engine(db_key=DCH_DATA_DB_KEY)

        # Define the schema explicitly (use "public" or your config)
        target_schema_name = "public"

        gdf.to_postgis(table_name, con=engine, schema=target_schema_name, if_exists="replace", index=False)

        with engine.begin() as conn:
            # Use double quotes for table name to handle special characters/case sensitivity
            conn.execute(text(f"""
                ALTER TABLE "{target_schema_name}"."{table_name}" 
                ADD COLUMN dch_id SERIAL PRIMARY KEY;
            """))

        # ------------------------------------------------------------------
        # Register vector layer metadata
        # ------------------------------------------------------------------
        DCHUtils.create_vector_layer_info(engine, table_name, uuid=uuid, layer_category_id=layer_category_id)

        success = True

        return {
            "payload": {
                "msg": "Shapefile uploaded and imported successfully",
                "uuid": uuid,
                "table": table_name,
                "upload_id": upload_id,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"PostGIS import error during shapefile upload: {e}")
        raise HTTPException(status_code=500, detail="Failed to import spatial data to database")
    finally:
        if success and upload_id:
            upload_service.cleanup_session(upload_id)


# ---------------------------------------------------------------------
# Shapefile upload -> PREVIEW append into existing table
# ---------------------------------------------------------------------
@dch_admin_router.post("/preview_append_shp/")
async def preview_append_shp(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_dch_admin(current_user)
    form = await request.form()
    layer_uuid = form.get("layerUUID")

    if not layer_uuid:
        raise HTTPException(status_code=400, detail="layerUUID is required")

    upload_service = DCHUploadSessionService(media_dir=DCH_MEDIA_DIR, logger=da_logger)
    package_service = DCHUploadPackageService(upload_service=upload_service)
    upload_id = None

    try:
        # -------------------------------------------------------------
        # 1. Resolve target layer metadata
        # -------------------------------------------------------------
        layer_info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
        if not layer_info:
            raise HTTPException(status_code=404, detail="Target layer not found")

        if not layer_info.data_model or layer_info.data_model.code != DataModel.V.name:
            raise HTTPException(status_code=400, detail="Target layer must be a vector layer")

        layer_access = layer_info.layer_accessibility
        if not layer_access:
            raise HTTPException(status_code=400, detail="Target layer accessibility missing")

        if (not layer_access.accessibility_type or layer_access.accessibility_type.code != AccessibilityType.DB.name):
            raise HTTPException(status_code=400, detail="Target layer is not a DB vector layer")

        target_table = layer_access.table_name
        target_schema_name = (layer_access.params or {}).get("schema", "public")

        # -------------------------------------------------------------
        # 2. Save uploaded files into generic upload session
        # -------------------------------------------------------------
        upload_id, session_dir, saved_files = await upload_service.save_files_from_form(form=form,
            upload_type="shapefile", metadata={"purpose": "vector_append_preview", "layer_uuid": layer_uuid, }, )

        # -------------------------------------------------------------
        # 3. Resolve and validate shapefile package
        # -------------------------------------------------------------
        shape_info = package_service.resolve_shapefile(upload_id)
        shp_fp = shape_info["main_file"]

        # -------------------------------------------------------------
        # 4. Load shapefile into GeoDataFrame
        # -------------------------------------------------------------
        gdf = append_service.load_shapefile_to_gdf(shp_fp)

        if gdf is None or gdf.empty:
            raise HTTPException(status_code=400, detail="Uploaded shapefile contains no features")

        # -------------------------------------------------------------
        # 5. Fetch target database table schema
        # -------------------------------------------------------------
        engine = layer_access.get_external_engine()

        target_schema = append_service.get_target_table_schema(engine=engine, table_name=target_table,
            schema=target_schema_name, )

        # -------------------------------------------------------------
        # 6. Analyze append compatibility (with GEOMETRY support)
        # -------------------------------------------------------------
        analysis = append_service.analyze_append_candidate(gdf, target_schema)

        # Block if critical validation errors exist (e.g. strict incompatibilities)
        if analysis.get("is_compatible") is False and analysis.get("fatal_errors"):
            raise HTTPException(status_code=400, detail="; ".join(analysis["fatal_errors"]), )

        # -------------------------------------------------------------
        # 7. Prepare GeoJSON preview (Reproject to EPSG:4326)
        # -------------------------------------------------------------
        preview_gdf = gdf.copy()
        if preview_gdf.crs:
            try:
                if preview_gdf.crs.to_epsg() != 4326:
                    preview_gdf = preview_gdf.to_crs(epsg=4326)
            except Exception as e:
                da_logger.warning(f"CRS transform failed, overriding EPSG:4326: {e}")
                preview_gdf.set_crs(epsg=4326, allow_override=True)
        else:
            preview_gdf.set_crs(epsg=4326, allow_override=True)

        geojson_str = preview_gdf.to_json()
        preview_geojson = json.loads(geojson_str)

        # -------------------------------------------------------------
        # 8. Create zipped GeoJSON preview
        # -------------------------------------------------------------
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("preview.geojson", geojson_str)

        zip_buffer.seek(0)
        preview_geojson_zip = base64.b64encode(zip_buffer.read()).decode()

        # -------------------------------------------------------------
        # 9. Attach payload metadata
        # -------------------------------------------------------------
        analysis["upload_id"] = upload_id
        analysis["layer_uuid"] = layer_uuid
        analysis["target_table"] = target_table
        analysis["target_schema_name"] = target_schema_name
        analysis["preview_feature_count"] = len(preview_gdf)
        analysis["preview_geojson"] = preview_geojson
        analysis["preview_geojson_zip"] = preview_geojson_zip

        return {"payload": analysis}

    except HTTPException:
        raise

    except Exception as e:
        # Cleanup temp files on unhandled exceptions
        if upload_id:
            try:
                upload_service.cleanup_session(upload_id)
            except Exception:
                pass

        da_logger.exception(f"Append preview failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to analyze shapefile for append preview: {str(e)}", )


# ---------------------------------------------------------------------
# Shapefile upload -> COMMIT append into existing table
# ---------------------------------------------------------------------
@dch_admin_router.post("/commit_append_shp/")
async def commit_append_shp(request: Request, current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Commit append after frontend finalizes mapping.
                        Validate upload
                              ↓
                        Load shapefile
                              ↓
                        Reproject if required
                              ↓
                        Validate mapping
                              ↓
                        Append to existing DB table
                              ↓
                        Update layer extent
                              ↓
                        appended_count > 0?
                              │
                              └── YES → clear MVT layer cache
                              ↓
                        Cleanup upload
                              ↓
                        Return success
    Expected multipart/form-data:
      - upload_id
      - layer_uuid
      - dch_id
      - final_mapping (JSON string: target -> source)
      - reproject_if_needed ("true"/"false")

    Protected endpoint.
    """
    require_dch_admin(current_user)
    form = await request.form()

    # Extracting parameters
    upload_id = form.get("upload_id")
    layer_uuid = form.get("layer_uuid")  # Added this to resolve the correct engine
    final_mapping_raw = form.get("final_mapping")
    reproject_if_needed = str(form.get("reproject_if_needed", "true")).lower() == "true"

    if not all([upload_id, layer_uuid, final_mapping_raw]):
        raise HTTPException(status_code=400, detail="Missing required parameters")

    try:
        final_mapping = json.loads(final_mapping_raw)
        # layer_info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
        # layer_access = layer_info.layer_accessibility
        layer_info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
        if not layer_info:
            raise HTTPException(status_code=404, detail="Target layer not found", )

        layer_access = layer_info.layer_accessibility
        if not layer_access:
            raise HTTPException(status_code=400, detail="Layer accessibility is not configured", )
        layer_srid = layer_info.srid
        # Use the layer-specific engine instead of generic DBBase
        engine = layer_access.get_external_engine()
        target_table = layer_access.table_name
        target_schema_name = (layer_access.params or {}).get("schema", "public")

        # 1. Locate session folder (Recursive search handles the date subfolder automatically)
        file_dir, shp_fp = append_service.get_shp_path_from_upload_id(upload_id)

        # 2. Process Data
        gdf = append_service.load_shapefile_to_gdf(shp_fp)
        if gdf.crs is None:
            raise HTTPException(status_code=400, detail="Uploaded shapefile has no CRS information", )
        current_epsg = gdf.crs.to_epsg()
        if current_epsg != layer_srid:
            try:
                da_logger.info(f"Reprojecting GDF from EPSG:{current_epsg} to EPSG:{layer_srid}")
                # Assignment is required here
                gdf = gdf.to_crs(epsg=layer_srid)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to reproject data to EPSG:{layer_srid}: {str(e)}")
        target_schema = append_service.get_target_table_schema(engine=engine, table_name=target_table,
            schema=target_schema_name)

        # 3. Validation & Preparation
        append_service.validate_final_mapping(final_mapping, append_service.get_source_schema(gdf), target_schema)

        prepared_gdf = append_service.prepare_gdf_for_append(gdf=gdf, final_mapping=final_mapping,
            target_schema=target_schema, reproject_if_needed=reproject_if_needed)

        # 4. Transactional Append
        # Check existing data count BEFORE we push new records
        existing_row_count = layer_info.count_layer_rows()

        appended_count = append_service.append_gdf_to_postgis(gdf=prepared_gdf, engine=engine, table_name=target_table,
            schema=target_schema_name)

        # 5. Update Layer Extent
        try:
            # total_bounds is [minx, miny, maxx, maxy]
            new_bounds = prepared_gdf.total_bounds.tolist()
            da_logger.info(f"New bounds: {new_bounds}")
            if new_bounds:
                # If existing_row_count was 0, the current extent is the Province fallback.
                # Overwrite it so the map 'snaps' to the new real data.
                should_overwrite = (existing_row_count == 0)

                layer_info.update_extent(new_bounds, overwrite=should_overwrite)
                da_logger.info(f"Updated extent for layer {layer_uuid}. Overwrite: {should_overwrite}")

        except Exception as extent_err:
            # We log but don't fail the request because the PostGIS data is already committed.
            da_logger.exception(f"Failed to update layer extent metadata: {extent_err}")

        # Cleanup on success
        if appended_count > 0:
            try:
                cleared_tiles = mvt_cache.clear_layer_cache(layer_uuid)

                da_logger.info(f"[MVTCacheClear] "
                               f"Features appended | "
                               f"layer={layer_uuid} | "
                               f"rows={appended_count} | "
                               f"tiles={cleared_tiles}")

            except Exception as cache_err:
                da_logger.warning(f"[MVTCacheClearError] "
                                  f"Layer data appended but cache clear failed | "
                                  f"layer={layer_uuid}: {cache_err}")
        append_service.cleanup_upload_dir(file_dir)

        return {"payload": {"msg": "Data successfully appended", "appended_rows": appended_count,
            "target": f"{target_schema_name}.{target_table}"}}

    except HTTPException:
        raise

    except Exception as e:
        da_logger.exception(f"Append commit failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------
# Feature Attribute Updates & Deletions
# ---------------------------------------------------------------------
@dch_admin_router.post("/update_layer_attributes/{uuid}/")
async def update_layer_attribute(uuid: str, payload: dict = Body(...),
        # Expects {"pk_object": {"id": 1, ...}, "column": "col_name", "value": "new_val"}
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Updates a single attribute for a feature in an external vector layer table.
    Requires app_manager or superuser privileges.
    """
    # 1. Authority Check
    require_dch_admin(current_user)

    pk_object = payload.get("pk_object")
    col_name = payload.get("column")
    new_value = payload.get("value")

    if not isinstance(pk_object, dict) or not pk_object:
        raise HTTPException(status_code=400, detail="Missing or invalid pk_object")

    if not col_name:
        raise HTTPException(status_code=400, detail="Missing column name")

    # 2. Fetch Layer Metadata & Accessibility
    with LayerInfo.managed_session() as session:
        info = session.execute(select(LayerInfo).options(selectinload(LayerInfo.layer_accessibility)).where(
            LayerInfo.uuid == uuid)).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found or accessibility info missing")

    access = info.layer_accessibility

    # 3. Security Validation: updated column must be allowed
    allowed_cols = set(access.cols_name or [])
    if col_name not in allowed_cols:
        raise HTTPException(status_code=400,
            detail=f"Column '{col_name}' is not in the allowed list or is not editable.")

    # 4. Validate PK object against configured PK columns
    pk_cols = access.pk_cols or []
    if not pk_cols:
        raise HTTPException(status_code=400, detail="No primary key columns configured for this layer")

    missing_pk_cols = [col for col in pk_cols if col not in pk_object]
    if missing_pk_cols:
        raise HTTPException(status_code=400, detail=f"Missing PK field(s): {', '.join(missing_pk_cols)}")

    extra_pk_cols = [col for col in pk_object.keys() if col not in pk_cols]
    if extra_pk_cols:
        raise HTTPException(status_code=400, detail=f"Unexpected PK field(s): {', '.join(extra_pk_cols)}")

    # Optional hardening: prevent PK columns from being edited
    if col_name in pk_cols:
        raise HTTPException(status_code=400, detail=f"Primary key column '{col_name}' cannot be updated.")

    # 5. Build dynamic SQL with bound params
    params = {"new_val": new_value}
    clauses = []

    for col in pk_cols:
        pname = f"pk_{col}"
        clauses.append(f"{DBQuery.safe_ident(col)} = :{pname}")
        params[pname] = pk_object[col]

    where_sql = " AND ".join(clauses)

    # 6. Get external engine
    engine = access.get_external_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="External database engine could not be initialized")

    table = DBQuery.safe_ident(access.table_name)
    target_col = DBQuery.safe_ident(col_name)

    schema_name = (access.params or {}).get("schema", "public")
    full_table_path = f"{DBQuery.safe_ident(schema_name)}.{table}"

    update_query = text(
        f"UPDATE {full_table_path} SET {DBQuery.safe_ident(col_name)} = :new_val WHERE {' AND '.join(clauses)}"
    )

    try:
        with Session(engine) as db_session:
            result = db_session.execute(update_query, params)
            db_session.commit()

            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Update failed: No matching feature found in the database.")

            pk_summary = ", ".join([f"{col}={pk_object[col]}" for col in pk_cols])
            # Clear Cache logic remains the same
            cleared_tiles = 0
            try:
                cleared_tiles = mvt_cache.clear_layer_cache(uuid)
            except Exception:
                pass

            return {"payload": {"status": "success",
                "message": f"Successfully updated '{col_name}' for feature ({pk_summary})",
                "rows_affected": result.rowcount}}



    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Attribute update failed for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail=f"Database Update Error: {str(e)}")


@dch_admin_router.post("/delete_layer_features/{uuid}/")
async def delete_layer_features(
    uuid: str,
    payload: dict = Body(...),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_dch_admin(current_user)

    pk_objects = payload.get("pk_objects", [])
    if not pk_objects or not isinstance(pk_objects, list):
        raise HTTPException(status_code=400, detail="No feature data provided for deletion")

    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found or accessibility info missing")

    access = info.layer_accessibility
    pk_cols = access.pk_cols or []
    if not pk_cols:
        raise HTTPException(status_code=400, detail="Primary keys not defined for this layer")

    engine = access.get_external_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Database engine could not be initialized")

    table = DBQuery.safe_ident(access.table_name)
    schema_name = (access.params or {}).get("schema", "public")
    full_path = f"{DBQuery.safe_ident(schema_name)}.{table}"

    query_params: dict[str, Any] = {}

    if len(pk_cols) == 1:
        pk_col_name = pk_cols[0]
        flat_values = []
        for idx, obj in enumerate(pk_objects):
            if not isinstance(obj, dict) or pk_col_name not in obj or obj[pk_col_name] is None:
                raise HTTPException(status_code=400, detail=f"Missing or null PK '{pk_col_name}' at index {idx}")
            flat_values.append(obj[pk_col_name])

        safe_pk = DBQuery.safe_ident(pk_col_name)
        delete_query = text(f"DELETE FROM {full_path} WHERE {safe_pk} IN :values")
        query_params["values"] = tuple(flat_values)
    else:
        safe_pk_cols = f"({', '.join(DBQuery.safe_ident(c) for c in pk_cols)})"
        row_clauses = []
        for r_idx, obj in enumerate(pk_objects):
            if not isinstance(obj, dict):
                raise HTTPException(status_code=400, detail=f"Invalid pk_object at index {r_idx}")
            col_params = []
            for c_idx, c in enumerate(pk_cols):
                if c not in obj or obj[c] is None:
                    raise HTTPException(status_code=400, detail=f"Missing or null PK '{c}' at index {r_idx}")
                p_key = f"dp_{r_idx}_{c_idx}"
                col_params.append(f":{p_key}")
                query_params[p_key] = obj[c]
            row_clauses.append(f"({', '.join(col_params)})")

        tuple_list_sql = ", ".join(row_clauses)
        delete_query = text(f"DELETE FROM {full_path} WHERE {safe_pk_cols} IN ({tuple_list_sql})")

    try:
        with Session(engine) as db_session:
            result = db_session.execute(delete_query, query_params)
            db_session.commit()
            rows_affected = result.rowcount or 0

        cleared_tiles = 0
        try:
            cleared_tiles = mvt_cache.clear_layer_cache(uuid)
        except Exception:
            pass

        return {
            "payload": {
                "status": "success",
                "rows_affected": rows_affected,
                "cache_cleared": True,
                "cleared_tiles": cleared_tiles,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Bulk delete failed for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail="Database error during deletion")


@dch_admin_router.post("/bulk_update_layer_field_value/{uuid}/")
async def bulk_update_layer_attribute(
    uuid: str,
    payload: dict = Body(...),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Bulk update a single attribute for multiple features in an external vector layer table.
    Supports single and composite primary keys with safe parameterized SQL.
    """
    require_dch_admin(current_user)

    pk_objects = payload.get("pk_objects", [])
    col_name = payload.get("column")
    new_value = payload.get("value")

    if not isinstance(pk_objects, list) or not pk_objects:
        raise HTTPException(status_code=400, detail="Missing or invalid pk_objects")

    if not col_name:
        raise HTTPException(status_code=400, detail="Missing column name")

    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found or accessibility info missing")

    access = info.layer_accessibility

    allowed_cols = set(access.cols_name or [])
    if col_name not in allowed_cols:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{col_name}' is not in the allowed list or is not editable.",
        )

    pk_cols = access.pk_cols or []
    if not pk_cols:
        raise HTTPException(status_code=400, detail="No primary key columns configured for this layer")
    if col_name in pk_cols:
        raise HTTPException(status_code=400, detail=f"Primary key column '{col_name}' cannot be updated.")

    engine = access.get_external_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="External database engine could not be initialized")

    table = DBQuery.safe_ident(access.table_name)
    target_col = DBQuery.safe_ident(col_name)
    schema_name = (access.params or {}).get("schema", "public")
    full_table_path = f"{DBQuery.safe_ident(schema_name)}.{table}"

    query_params: dict[str, Any] = {"new_val": new_value}

    # Build parameterized WHERE clause
    if len(pk_cols) == 1:
        pk_col_name = pk_cols[0]
        flat_values = []
        for idx, obj in enumerate(pk_objects):
            if not isinstance(obj, dict) or pk_col_name not in obj:
                raise HTTPException(status_code=400, detail=f"Missing PK '{pk_col_name}' at index {idx}")
            v = obj[pk_col_name]
            if v is None:
                raise HTTPException(status_code=400, detail=f"PK value cannot be None at index {idx}")
            flat_values.append(v)

        pk_sql = DBQuery.safe_ident(pk_col_name)
        update_query = text(
            f"UPDATE {full_table_path} SET {target_col} = :new_val WHERE {pk_sql} IN :pk_values"
        )
        query_params["pk_values"] = tuple(flat_values)
    else:
        # Composite PK: Construct WHERE (pk1, pk2) IN ((:p0_0, :p0_1), (:p1_0, :p1_1), ...)
        safe_pk_cols = f"({', '.join(DBQuery.safe_ident(c) for c in pk_cols)})"
        row_clauses = []
        for r_idx, obj in enumerate(pk_objects):
            if not isinstance(obj, dict):
                raise HTTPException(status_code=400, detail=f"Invalid pk_object at index {r_idx}")
            col_params = []
            for c_idx, c in enumerate(pk_cols):
                if c not in obj or obj[c] is None:
                    raise HTTPException(status_code=400, detail=f"Missing or null PK '{c}' at index {r_idx}")
                p_key = f"p_{r_idx}_{c_idx}"
                col_params.append(f":{p_key}")
                query_params[p_key] = obj[c]
            row_clauses.append(f"({', '.join(col_params)})")

        tuple_list_sql = ", ".join(row_clauses)
        update_query = text(
            f"UPDATE {full_table_path} SET {target_col} = :new_val WHERE {safe_pk_cols} IN ({tuple_list_sql})"
        )

    try:
        with Session(engine) as db_session:
            result = db_session.execute(update_query, query_params)
            db_session.commit()
            rows_affected = result.rowcount or 0

        cleared_tiles = 0
        try:
            cleared_tiles = mvt_cache.clear_layer_cache(uuid)
        except Exception as cache_err:
            da_logger.warning(f"Failed to clear tile cache for {uuid}: {cache_err}")

        return {
            "payload": {
                "status": "success",
                "message": f"Successfully updated '{col_name}' for {rows_affected} feature(s)",
                "rows_affected": rows_affected,
                "pk_count": len(pk_objects),
                "cache_cleared": True,
                "cleared_tiles": cleared_tiles,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Bulk attribute update failed for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail=f"Database Update Error: {str(e)}")

# ---------------------------------------------------------------------
# Geometry Download & Upload
# ---------------------------------------------------------------------
@dch_admin_router.post("/download_geometry/{uuid}/")
async def download_rows_geometry(uuid: str, payload: dict = Body(...),
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    require_dch_admin(current_user)
    pk_objects = payload.get("pk_objects")
    if not isinstance(pk_objects, list) or not pk_objects:
        raise HTTPException(status_code=400, detail="pk_objects must be a non-empty list", )

    try:
        result = LayerQueryService.get_features_geojson_by_pk_objects(
            identifier=uuid, pk_objects=pk_objects, by="uuid"
        )
        if isinstance(result, Response):
            return result

        geojson: GeoJSON = result
        if not geojson.features:
            raise HTTPException(status_code=404, detail="No geometry found for selected row(s)")

        gdf = geojson.to_gdf()
        if gdf.empty:
            raise HTTPException(status_code=404, detail="No valid geometry found for selected row(s)")

        tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=True)
        tmp_path = Path(tmp.name)
        tmp.close()

        # layer_name = f"selected_geometry_{uuid[:8]}"
        # safe_layer_name = layer_name[:60]
        safe_layer_name = tmp_path.stem.replace(" ", "_")
        gdf.to_file(tmp_path, layer=safe_layer_name, driver="GPKG")

        return FileResponse(
            path=str(tmp_path),
            media_type="application/geopackage+sqlite3",
            filename=f"{safe_layer_name}.gpkg",
        )
    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to download geometry for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to download selected geometry")


def validate_geometry_bounds(gdf, source_srid: int):
    if gdf.empty:
        raise HTTPException(status_code=400, detail="Uploaded GeoJSON has no geometry")

    minx, miny, maxx, maxy = [float(v) for v in gdf.total_bounds]

    if source_srid == 4326:
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise HTTPException(status_code=400, detail=("Invalid coordinates for WGS84 / EPSG:4326. "
                                                         "Longitude must be between -180 and 180, "
                                                         "latitude must be between -90 and 90."), )

    elif source_srid == 3857:
        limit = 20037508.342789244

        if minx < -limit or maxx > limit or miny < -limit or maxy > limit:
            raise HTTPException(status_code=400, detail=("Invalid coordinates for Web Mercator / EPSG:3857. "
                                                         "Coordinates must be within ±20037508.34 meters."), )

    elif source_srid == 32642:
        # UTM Zone 42N: easting normally 166021–833978, northing 0–9329005
        if minx < 100000 or maxx > 900000 or miny < 0 or maxy > 10000000:
            raise HTTPException(status_code=400, detail=("Invalid coordinates for UTM Zone 42N / EPSG:32642. "
                                                         "Expected easting roughly 100000–900000 and northing 0–10000000."), )

    elif source_srid == 32643:
        # UTM Zone 43N: easting normally 166021–833978, northing 0–9329005
        if minx < 100000 or maxx > 900000 or miny < 0 or maxy > 10000000:
            raise HTTPException(status_code=400, detail=("Invalid coordinates for UTM Zone 43N / EPSG:32643. "
                                                         "Expected easting roughly 100000–900000 and northing 0–10000000."), )


@dch_admin_router.post("/upload_geometry/{uuid}/")
async def upload_row_geometry(uuid: str, request: Request,
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    require_dch_admin(current_user)

    form = await request.form()

    upload_file = form.get("file")
    pk_object_raw = form.get("pk_object")
    source_srid_raw = form.get("source_srid") or "4326"

    if not upload_file or not pk_object_raw:
        raise HTTPException(status_code=400, detail="file and pk_object are required")

    file_name = upload_file.filename or ""
    if not file_name.lower().endswith((".geojson", ".json")):
        raise HTTPException(status_code=400, detail="Only GeoJSON file is supported")

    try:
        pk_object = json.loads(str(pk_object_raw))
    except Exception:
        raise HTTPException(status_code=400, detail="pk_object must be valid JSON")

    if not isinstance(pk_object, dict) or not pk_object:
        raise HTTPException(status_code=400, detail="pk_object must be a non-empty object")

    try:
        source_srid = int(source_srid_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="source_srid must be integer")

    allowed_srid = {4326, 3857, 32642, 32643}

    if source_srid not in allowed_srid:
        raise HTTPException(status_code=400, detail="Unsupported source_srid")

    layer_info = LayerInfo.get_layer_info_by_uuid(uuid)

    if not layer_info or not layer_info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found")

    access = layer_info.layer_accessibility

    if layer_info.data_model.code != DataModel.V.name:
        raise HTTPException(status_code=400, detail="Layer is not a vector layer")

    if access.accessibility_type.code != AccessibilityType.DB.name:
        raise HTTPException(status_code=400, detail="Layer is not a DB layer")

    pk_cols = access.pk_cols or []

    if not pk_cols:
        raise HTTPException(status_code=400, detail="Layer primary key is not configured")

    missing_pk_cols = [col for col in pk_cols if col not in pk_object]

    if missing_pk_cols:
        raise HTTPException(status_code=400, detail=f"Missing PK field(s): {', '.join(missing_pk_cols)}")

    extra_pk_cols = [col for col in pk_object.keys() if col not in pk_cols]

    if extra_pk_cols:
        raise HTTPException(status_code=400, detail=f"Unexpected PK field(s): {', '.join(extra_pk_cols)}")

    engine = access.get_external_engine()

    if engine is None:
        raise HTTPException(status_code=500, detail="Layer database engine could not be initialized")

    schema_name = (access.params or {}).get("schema", "public")
    table_name = access.table_name
    geom_col = access.geometry_col or "geom"
    target_srid = layer_info.srid or 4326

    if not table_name:
        raise HTTPException(status_code=400, detail="Layer table name is missing")

    try:
        file_bytes = await upload_file.read()
        geojson_dict = json.loads(file_bytes.decode("utf-8"))

        raw_crs = geojson_dict.get("crs")

        if isinstance(raw_crs, dict):
            crs_name = (raw_crs.get("properties", {}).get("name") or raw_crs.get("name"))

            if isinstance(crs_name, str):
                # Handles:
                # urn:ogc:def:crs:EPSG::3857
                # EPSG:3857
                if "EPSG::" in crs_name:
                    epsg = crs_name.split("EPSG::")[-1]
                    geojson_dict["crs"] = f"EPSG:{epsg}"
                elif "EPSG:" in crs_name:
                    epsg = crs_name.split("EPSG:")[-1]
                    geojson_dict["crs"] = f"EPSG:{epsg}"
                else:
                    geojson_dict["crs"] = f"EPSG:{source_srid}"
            else:
                geojson_dict["crs"] = f"EPSG:{source_srid}"

        elif not raw_crs:
            geojson_dict["crs"] = f"EPSG:{source_srid}"

        geojson = GeoJSON(**geojson_dict)

        if not geojson.features:
            raise HTTPException(status_code=400, detail="GeoJSON has no feature")

        if len(geojson.features) != 1:
            raise HTTPException(status_code=400, detail="GeoJSON must contain exactly one feature")

        if not geojson.crs:
            geojson.crs = f"EPSG:{source_srid}"

        gdf = geojson.to_gdf(assume_epsg=source_srid)

        if gdf.empty:
            raise HTTPException(status_code=400, detail="GeoJSON has no valid geometry")

        if len(gdf) != 1:
            raise HTTPException(status_code=400, detail="GeoJSON must contain exactly one valid geometry")

        validate_geometry_bounds(gdf, source_srid)

        if source_srid != target_srid:
            gdf = gdf.to_crs(epsg=target_srid)

        geom = gdf.geometry.iloc[0]

        if geom is None or geom.is_empty:
            raise HTTPException(status_code=400, detail="Uploaded geometry is empty")

        if not geom.is_valid:
            geom = geom.buffer(0)

        if geom is None or geom.is_empty or not geom.is_valid:
            raise HTTPException(status_code=400, detail="Uploaded geometry is invalid")

        params = {"geom_wkt": geom.wkt, "srid": target_srid, }

        where_parts = []

        for idx, pk_col in enumerate(pk_cols):
            param_name = f"pk_{idx}"
            where_parts.append(f"{DBQuery.safe_ident(pk_col)} = :{param_name}")
            params[param_name] = pk_object[pk_col]

        safe_schema = DBQuery.safe_ident(schema_name)
        safe_table = DBQuery.safe_ident(table_name)
        safe_geom_col = DBQuery.safe_ident(geom_col)

        sql = text(f"""
            UPDATE {safe_schema}.{safe_table}
            SET {safe_geom_col} = ST_SetSRID(
                ST_GeomFromText(:geom_wkt),
                :srid
            )
            WHERE {" AND ".join(where_parts)}
        """)

        with Session(engine) as db_session:
            result = db_session.execute(sql, params)
            db_session.commit()

        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="No matching row found for geometry update")

        try:
            layer_info.update_extent(gdf.total_bounds.tolist(), overwrite=False)
        except Exception as e:
            da_logger.exception(f"Failed to update layer extent after geometry upload: {e}")

        try:
            mvt_cache.clear_layer_cache(uuid)
        except Exception:
            pass

        return {
            "payload": {
                "status": "success",
                "message": "Geometry uploaded successfully",
                "rows_affected": result.rowcount,
                "pk_object": pk_object,
                "source_srid": source_srid,
                "target_srid": target_srid,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to upload geometry for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload geometry")


# =====================================================================
# 1. Reconcile Layer Info by UUID
# =====================================================================
@dch_admin_router.post("/reconcile_layer_info/{uuid}/")
async def reconcile_layer_info_by_uuid(
    uuid: str,
    schema: str = Query("public", description="Database schema name"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Reconcile LayerInfo & LayerAccessibility metadata against real DB tables
    using the core manage.py reconciliation engine.
    """
    require_dch_admin(current_user)

    result = reconcile_layerinfo(layer_uuid=uuid, schema=schema, check=False)

    if result["checked_layers"] == 0:
        raise HTTPException(status_code=404, detail="Layer not found or inaccessible")

    if result["failed_layers"] > 0:
        err_msg = result["errors"][0]["error"] if result["errors"] else "Unknown error"
        raise HTTPException(status_code=500, detail=f"Reconciliation failed: {err_msg}")

    # Clear MVT cache for the updated layer
    try:
        mvt_cache.clear_layer_cache(uuid)
    except Exception as e:
        da_logger.warning(f"Failed to clear tile cache for {uuid}: {e}")

    return {
        "payload": {
            "status": "success",
            "message": f"Layer '{uuid}' reconciled successfully",
            "summary": result,
        }
    }

# =====================================================================
# 2. Create Layer Info from Model Name
# =====================================================================
@dch_admin_router.post("/create_layer_info_from_model/{model_name}/")
async def create_layer_info_from_model(
    model_name: str,
    category_id: int | None = Query(None, description="Optional LayerCategory ID"),
    overwrite: bool = Query(True, description="Overwrite existing LayerInfo if present"),
    include_non_spatial: bool = Query(False, description="Allow tables without geometries"),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Generate LayerInfo & LayerAccessibility records from a model name
    using create_layerinfo_from_app_models from manage.py.
    """
    require_dch_admin(current_user)

    try:
        summary = create_layerinfo_from_app_models(
            model_name=model_name,
            layer_category_id=category_id,
            overwrite=overwrite,
            include_non_spatial=include_non_spatial,
        )

        return {
            "payload": {
                "status": "success",
                "message": f"Layer creation from model '{model_name}' completed",
                "summary": summary,
            }
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        da_logger.exception(f"Failed to create layer from model '{model_name}': {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create layer: {str(e)}")
