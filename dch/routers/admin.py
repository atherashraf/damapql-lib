import json
import geopandas as gpd

import io
import zipfile
import base64
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import selectinload, Session

from app.damapql.dch.services.layer_info_services import LayerInfoService
from app.damapql.dch.utils.mvt_utils import MVTCache
from app.damapql.settings import DCH_MEDIA_DIR, DCH_DATA_DB_KEY

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils

from app.damapql.dch.dtypes import DBConnectionCreate
from app.damapql.dch.models import (
    AccessibilityType,
    DBConnection,
    DBQuery,
    DataModel,
    LayerAccessibility,
    LayerInfo,
)
from app.damapql.dch.services.vector_append_service import DCHVectorAppendService
from app.damapql.dch.services.upload_session_service import DCHUploadSessionService
from app.damapql.dch.services.upload_package_service import DCHUploadPackageService
from app.damapql.dch.utils.dch_utils import DCHUtils

from digitalarzengine.io.managers.db_manager import GeoDBManager

from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.deps import get_sync_session
from app.damapql.utils.adapters.model_utils import ModelUtils
from app.damapql.utils.crypto import CryptoUtils
from digitalarzengine.utils.singletons import da_logger


# ---------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------
dch_admin_router = APIRouter(prefix="/admin", tags=["dch admin"])


# ---------------------------------------------------------------------
# Shared append/upload service
# This service is used by:
#   1. upload_shp_file        -> create a NEW table
#   2. preview_append_shp     -> inspect append compatibility
#   3. commit_append_shp      -> append into EXISTING table
# ---------------------------------------------------------------------
append_service = DCHVectorAppendService(
    media_dir=DCH_MEDIA_DIR,
    logger=da_logger,
)


# ---------------------------------------------------------------------
# Auth helper
# We allow:
#   - superuser
#   - app_manager
# ---------------------------------------------------------------------
def require_dch_admin(current_user: UserInfo):
    if getattr(current_user, "is_superuser", False):
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required",
    )


# ---------------------------------------------------------------------
# DB Connection Endpoints
# ---------------------------------------------------------------------
@dch_admin_router.get("/get_db_connection/")
async def get_db_connection(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
def test_db_connection(
    payload: DBConnectionCreate,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
def add_db_connection(
    payload: DBConnectionCreate,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Save an external DB connection definition in the system.

    Password is encrypted before storing.

    Protected endpoint.
    """
    require_dch_admin(current_user)

    try:
        p = payload.params
        encrypted_password = CryptoUtils().encrypt_txt(p.password)

        db_conn = DBConnection.get_db_connection(
            con_name=payload.connection_name,
            engine=payload.engine,
            host=p.host,
            port=p.port,
            user=p.user,
            password=encrypted_password,
            db_name=p.name,
        )

        return {
            "payload": {
                "success": True,
                "msg": f"Connection '{db_conn.name}' added successfully",
                "data": {"id": db_conn.id, "name": db_conn.name},
            }
        }

    except Exception as e:
        da_logger.exception(f"Failed to add DB connection: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@dch_admin_router.get("/db_table_list/{db_id}/")
async def db_table_list(
    db_id: str, db = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
    layer_category_id: str,
    db = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Register an existing external DB spatial table as a layer in local metadata.

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

        is_created = DCHUtils.create_vector_layer_info(
            engine=engine,
            table_name=table_name,
            layer_category_id=layer_category_id,
            user=current_user,
            db_conn_name=obj.name
        )

        return {
            "payload": {
                "msg": "Layer info created" if is_created else "Failed to create Layer Info"
            }
        }

    except Exception as e:
        da_logger.exception(f"Failed to save DB layer info: {e}")
        raise HTTPException(status_code=500, detail="Failed to save DB layer info")

# @dch_admin_router.get("/save_db_layer_info/{db_id}/{table_name}/{layer_category_id}/")
# async def save_db_layer_info(
#     db_id: str,
#     table_name: str,
#     layer_category_id: str,
#     db=Depends(get_sync_session),
#     current_user: UserInfo = Depends(AuthUtils.get_current_user),
# ):
#     require_dch_admin(current_user)
#
#     qs = select(DBConnection).where(DBConnection.id == db_id)
#     result = db.execute(qs)
#     obj: DBConnection = result.scalar_one_or_none()
#
#     if obj is None:
#         raise HTTPException(status_code=404, detail="DB connection not found")
#
#     try:
#         service = LayerInfoService()
#
#         layer, changed = service.save_db_table_layer_info(
#             db=db,
#             engine=obj.get_sqlalchemy_engine(),
#             table_name=table_name,
#             layer_category_id=layer_category_id,
#             user=current_user,
#             db_conn_name=obj.name,
#             overwrite=False,
#         )
#
#         db.commit()
#
#         return {
#             "payload": {
#                 "msg": "Layer info created" if changed else "Layer info already exists",
#                 "uuid": getattr(layer, "uuid", None) if layer else None,
#             }
#         }
#
#     except Exception as e:
#         db.rollback()
#         da_logger.exception(f"Failed to save DB layer info: {e}")
#         raise HTTPException(status_code=500, detail="Failed to save DB layer info")

# ---------------------------------------------------------------------
# Generic model endpoints
# ---------------------------------------------------------------------
@dch_admin_router.post("/add_model_row/")
async def add_model_row(
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
async def save_model_data(
    request: Request,
    model_name: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
        obj = (
            session.query(model_class)
            .filter(getattr(model_class, pk_col_name) == pk_col_value)
            .first()
        )

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
    Create a URL-based layer metadata entry.

    Expected JSON:
    {
      "layer_title": "Layer 1",
      "layer_category_id": 1,
      "layer_url": "https://....",
      "url_type": "wms"
    }

    Protected endpoint.
    """
    require_dch_admin(current_user)

    data = await request.json()

    layer_title = data.get("layer_title")
    layer_category_id = data.get("layer_category_id")
    layer_url = data.get("layer_url")
    url_type = data.get("url_type")

    if not layer_title:
        raise HTTPException(status_code=400, detail="layer_title is required")
    if not layer_category_id:
        raise HTTPException(status_code=400, detail="layer_category_id is required")
    if not layer_url:
        raise HTTPException(status_code=400, detail="layer_url is required")
    if not url_type:
        raise HTTPException(status_code=400, detail="url_type is required")

    try:
        with LayerInfo.managed_session() as session:
            accessibility = LayerAccessibility(
                accessibility_type=AccessibilityType.URL.name,
                params={
                    "url": layer_url,
                    "url_type": url_type,
                },
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

        return {"payload": {"msg": "Layer URL info added successfully"}}

    except Exception as e:
        da_logger.exception(f"Failed to add URL layer info: {e}")
        raise HTTPException(status_code=500, detail="Failed to add layer URL info")


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
      "rowData": {"title": "Updated title"}
    }

    Protected endpoint.
    """
    require_dch_admin(current_user)

    data = await request.json()
    pk_col = data.get("pk", {}).get("colName")
    pk_val = data.get("pk", {}).get("colValue")
    row_data = data.get("rowData")

    if not pk_col or pk_val is None or not isinstance(row_data, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    if not hasattr(LayerInfo, pk_col):
        raise HTTPException(status_code=400, detail=f"Invalid LayerInfo PK column: {pk_col}")

    try:
        with LayerInfo.managed_session() as session:
            obj = session.execute(
                select(LayerInfo).where(getattr(LayerInfo, pk_col) == pk_val)
            ).scalar_one_or_none()

            if obj is None:
                raise HTTPException(status_code=404, detail="LayerInfo not found")

            for key, value in row_data.items():
                if hasattr(obj, key):
                    if key == "data_model":
                        value = DataModel.get_name(value)
                    setattr(obj, key, value)

            obj.save(session=session)

        return {"payload": {"status": "success", "updated": row_data}}

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to save layer info: {e}")
        raise HTTPException(status_code=500, detail="Failed to save layer info")


# ---------------------------------------------------------------------
# Shapefile upload -> create NEW table
# ---------------------------------------------------------------------
@dch_admin_router.post("/upload_shp_file/")
async def upload_shp_file(
    request: Request,
    current_user: dict = Depends(AuthUtils.require_role("app_manager"))
):
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

    upload_service = DCHUploadSessionService(
        media_dir=DCH_MEDIA_DIR,
        logger=da_logger,
    )
    package_service = DCHUploadPackageService(upload_service=upload_service)

    upload_id = None
    success = False

    try:
        # ------------------------------------------------------------------
        # Save uploaded files into a generic upload session
        # ------------------------------------------------------------------
        upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
            form=form,
            upload_type="shapefile",
            metadata={"purpose": "vector_import"},
        )

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

        # ------------------------------------------------------------------
        # Import to PostGIS
        # ------------------------------------------------------------------
        # Use 'schema=' argument to ensure it goes exactly where you want
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
        DCHUtils.create_vector_layer_info(
            engine,
            table_name,
            uuid=uuid,
            layer_category_id=layer_category_id
        )

        success = True

        return {
            "payload": {
                "msg": "Shapefile uploaded, imported successfully, and temporary files removed",
                "uuid": uuid,
                "table": table_name,
                "upload_id": upload_id,
            }
        }

    except HTTPException:
        raise

    except Exception as e:
        da_logger.exception(f"PostGIS import error during shapefile upload: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to import spatial data to database"
        )

    finally:
        # --------------------------------------------------------------
        # Cleanup temp upload session only after successful import
        # --------------------------------------------------------------
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

    upload_service = DCHUploadSessionService(
        media_dir=DCH_MEDIA_DIR,
        logger=da_logger,
    )
    package_service = DCHUploadPackageService(upload_service=upload_service)

    upload_id = None

    try:
        # -------------------------------------------------------------
        # Resolve target layer
        # -------------------------------------------------------------
        layer_info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
        if not layer_info:
            raise HTTPException(status_code=404, detail="Target layer not found")

        if not layer_info.data_model or layer_info.data_model.code != DataModel.V.name:
            raise HTTPException(status_code=400, detail="Target layer must be a vector layer")

        layer_access = layer_info.layer_accessibility
        if not layer_access:
            raise HTTPException(status_code=400, detail="Target layer accessibility missing")

        if not layer_access.accessibility_type or layer_access.accessibility_type.code != AccessibilityType.DB.name:
            raise HTTPException(status_code=400, detail="Target layer is not a DB vector layer")

        target_table = layer_access.table_name
        target_schema_name = (layer_access.params or {}).get("schema", "public")

        # -------------------------------------------------------------
        # Save uploaded files into generic upload session
        # -------------------------------------------------------------
        upload_id, session_dir, saved_files = await upload_service.save_files_from_form(
            form=form,
            upload_type="shapefile",
            metadata={
                "purpose": "vector_append_preview",
                "layer_uuid": layer_uuid,
            },
        )

        # -------------------------------------------------------------
        # Resolve and validate shapefile package
        # -------------------------------------------------------------
        shape_info = package_service.resolve_shapefile(upload_id)
        shp_fp = shape_info["main_file"]

        # -------------------------------------------------------------
        # Load shapefile
        # -------------------------------------------------------------
        gdf = append_service.load_shapefile_to_gdf(shp_fp)

        if gdf is None or gdf.empty:
            raise HTTPException(status_code=400, detail="Uploaded shapefile contains no features")

        # -------------------------------------------------------------
        # Get target schema
        # -------------------------------------------------------------
        engine = layer_access.get_external_engine()

        target_schema = append_service.get_target_table_schema(
            engine=engine,
            table_name=target_table,
            schema=target_schema_name,
        )

        # -------------------------------------------------------------
        # Analyze append compatibility
        # -------------------------------------------------------------
        analysis = append_service.analyze_append_candidate(gdf, target_schema)

        # -------------------------------------------------------------
        # Prepare GeoJSON preview
        # Reproject to EPSG:4326 for safe web preview
        # -------------------------------------------------------------
        preview_gdf = gdf.copy()

        if preview_gdf.crs:
            try:
                if preview_gdf.crs.to_epsg() != 4326:
                    preview_gdf = preview_gdf.to_crs(epsg=4326)
            except Exception as e:
                # da_logger.exception(str(e))
                preview_gdf = preview_gdf.to_crs(epsg=4326)

        geojson_str = preview_gdf.to_json()
        preview_geojson = json.loads(geojson_str)

        # -------------------------------------------------------------
        # Create zipped GeoJSON preview
        # Useful if frontend wants compressed transfer/storage
        # -------------------------------------------------------------
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("preview.geojson", geojson_str)

        zip_buffer.seek(0)
        preview_geojson_zip = base64.b64encode(zip_buffer.read()).decode()

        # -------------------------------------------------------------
        # Attach extra response fields
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
        # Cleanup failed temp upload session
        if upload_id:
            upload_service.cleanup_session(upload_id)

        da_logger.exception(f"Append preview failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyze shapefile for append preview")


# ---------------------------------------------------------------------
# Shapefile upload -> COMMIT append into existing table
# ---------------------------------------------------------------------
@dch_admin_router.post("/commit_append_shp/")
async def commit_append_shp(
        request: Request,
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Commit append after frontend finalizes mapping.

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
        layer_info = LayerInfo.get_layer_info_by_uuid(layer_uuid)
        layer_access = layer_info.layer_accessibility
        layer_srid = layer_info.srid
        # Use the layer-specific engine instead of generic DBBase
        engine = layer_access.get_external_engine()
        target_table = layer_access.table_name
        target_schema_name = (layer_access.params or {}).get("schema", "public")

        # 1. Locate session folder (Recursive search handles the date subfolder automatically)
        file_dir, shp_fp = append_service.get_shp_path_from_upload_id(upload_id)

        # 2. Process Data
        gdf = append_service.load_shapefile_to_gdf(shp_fp)
        current_epsg = gdf.crs.to_epsg()

        if current_epsg != layer_srid:
            try:
                da_logger.info(f"Reprojecting GDF from EPSG:{current_epsg} to EPSG:{layer_srid}")
                # Assignment is required here
                gdf = gdf.to_crs(epsg=layer_srid)
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to reproject data to EPSG:{layer_srid}: {str(e)}"
                )
        target_schema = append_service.get_target_table_schema(
            engine=engine, table_name=target_table, schema=target_schema_name
        )

        # 3. Validation & Preparation
        append_service.validate_final_mapping(final_mapping, append_service.get_source_schema(gdf), target_schema)

        prepared_gdf = append_service.prepare_gdf_for_append(
            gdf=gdf,
            final_mapping=final_mapping,
            target_schema=target_schema,
            reproject_if_needed=reproject_if_needed
        )

        # 4. Transactional Append
        appended_count = append_service.append_gdf_to_postgis(
            gdf=prepared_gdf, engine=engine, table_name=target_table, schema=target_schema_name
        )

        # 5. Update Layer Extent
        try:
            # Calculate bounds of the newly added data
            # returns (minx, miny, maxx, maxy)
            new_bounds = prepared_gdf.total_bounds.tolist()

            # Update the LayerInfo record with the combined extent
            layer_info.update_extent(new_bounds)
            da_logger.info(f"Updated extent for layer {layer_uuid}")
        except Exception as extent_err:
            # We log but don't fail the whole request,
            # as the data is already committed to PostGIS
            da_logger.error(f"Failed to update layer extent: {extent_err}")

        # Cleanup on success
        append_service.cleanup_upload_dir(file_dir)

        return {
            "payload": {
                "msg": "Data successfully appended",
                "appended_rows": appended_count,
                "target": f"{target_schema_name}.{target_table}"
            }
        }

    except Exception as e:
        da_logger.exception(f"Append commit failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))



from fastapi import Body, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

@dch_admin_router.post("/update_layer_attributes/{uuid}/")
async def update_layer_attribute(
    uuid: str,
    payload: dict = Body(...),  # Expects {"pk_object": {"id": 1, ...}, "column": "col_name", "value": "new_val"}
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
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
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found or accessibility info missing")

    access = info.layer_accessibility

    # 3. Security Validation: updated column must be allowed
    allowed_cols = set(access.cols_name or [])
    if col_name not in allowed_cols:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{col_name}' is not in the allowed list or is not editable."
        )

    # 4. Validate PK object against configured PK columns
    pk_cols = access.pk_cols or []
    if not pk_cols:
        raise HTTPException(status_code=400, detail="No primary key columns configured for this layer")

    missing_pk_cols = [col for col in pk_cols if col not in pk_object]
    if missing_pk_cols:
        raise HTTPException(
            status_code=400,
            detail=f"Missing PK field(s): {', '.join(missing_pk_cols)}"
        )

    extra_pk_cols = [col for col in pk_object.keys() if col not in pk_cols]
    if extra_pk_cols:
        raise HTTPException(
            status_code=400,
            detail=f"Unexpected PK field(s): {', '.join(extra_pk_cols)}"
        )

    # Optional hardening: prevent PK columns from being edited
    if col_name in pk_cols:
        raise HTTPException(
            status_code=400,
            detail=f"Primary key column '{col_name}' cannot be updated."
        )

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
        f"UPDATE {full_table_path} "
        f"SET {target_col} = :new_val "
        f"WHERE {where_sql}"
    )

    try:
        with Session(engine) as db_session:
            result = db_session.execute(update_query, params)
            db_session.commit()

            if result.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="Update failed: No matching feature found in the database."
                )

            pk_summary = ", ".join([f"{col}={pk_object[col]}" for col in pk_cols])
            # Clear Cache logic remains the same
            cleared_tiles = 0
            try:
                cleared_tiles = MVTCache.clear_layer_cache(uuid)
            except Exception:
                pass

            return {
                "payload": {
                    "status": "success",
                    "message": f"Successfully updated '{col_name}' for feature ({pk_summary})",
                    "rows_affected": result.rowcount
                }
            }



    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Attribute update failed for layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail=f"Database Update Error: {str(e)}")





@dch_admin_router.post("/delete_layer_features/{uuid}/")
async def delete_layer_features(
        uuid: str,
        payload: dict = Body(...),  # Expects {"pk_objects": [{"id": 1, "code": "A"}, ...]}
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    require_dch_admin(current_user)

    # 1. Get objects from payload
    pk_objects = payload.get("pk_objects", [])
    if not pk_objects:
        raise HTTPException(status_code=400, detail="No feature data provided for deletion")

    with LayerInfo.managed_session() as session:
        info = session.execute(
            select(LayerInfo)
            .options(selectinload(LayerInfo.layer_accessibility))
            .where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

    if not info or not info.layer_accessibility:
        raise HTTPException(status_code=404, detail="Layer not found")

    access = info.layer_accessibility
    pk_cols = access.pk_cols or []

    if not pk_cols:
        raise HTTPException(status_code=400, detail="Primary keys not defined for this layer")

    # 2. Parse Objects into Ordered Tuples based on pk_cols definition
    try:
        parsed_values = []
        for obj in pk_objects:
            # Create a tuple of values in the order of the PK columns
            row_tuple = tuple(obj.get(col) for col in pk_cols)

            # Basic validation: Ensure no PK value is None
            if any(v is None for v in row_tuple):
                raise ValueError(f"Missing PK value in object: {obj}")

            parsed_values.append(row_tuple)

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid data format: {str(e)}")

    # 3. Construct the SQL
    safe_pk_cols = [DBQuery.safe_ident(c) for c in pk_cols]

    # Format for PostgreSQL: WHERE (col1, col2) IN ((val1, val2), (val3, val4))
    if len(safe_pk_cols) > 1:
        pk_cols_sql = f"({', '.join(safe_pk_cols)})"
    else:
        pk_cols_sql = safe_pk_cols[0]

    engine = access.get_external_engine()
    table = DBQuery.safe_ident(access.table_name)
    # Get schema from params or default to public
    schema_name = (access.params or {}).get("schema", "public")
    full_path = f"{DBQuery.safe_ident(schema_name)}.{table}"

    # Use tuple of tuples for the 'IN' parameter
    delete_query = text(f"DELETE FROM {full_path} WHERE {pk_cols_sql} IN :values")

    try:
        with Session(engine) as db_session:
            # We wrap the list in a tuple to ensure SQLAlchemy treats it as a single parameter for IN
            result = db_session.execute(delete_query, {"values": tuple(parsed_values)})
            db_session.commit()

        # Clear Cache logic remains the same
        cleared_tiles = 0
        try:
            cleared_tiles = MVTCache.clear_layer_cache(uuid)
        except Exception:
            pass

        return {
            "payload": {
                "status": "success",
                "rows_affected": result.rowcount,
                "cache_cleared": True
            }
        }

    except Exception as e:
        da_logger.exception(f"Bulk delete failed: {e}")
        raise HTTPException(status_code=500, detail="Database error during deletion")