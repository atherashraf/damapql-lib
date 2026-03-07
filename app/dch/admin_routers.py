from urllib.parse import quote_plus

from digitalarzengine.io.managers.db_manager import GeoDBManager
from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import create_engine, text
from starlette.responses import Response
import json

from app.dch.dtypes import DBConnectionCreate
from app.dch.models import DBConnection
from app.dch.utils.dch_utils import DCHUtils
from utils.adapters.model_utils import ModelUtils
from utils.crypto import CryptoUtils
from utils.singletons import da_logger
from fastapi import Depends
from app.auth.utils import AuthUtils

dch_admin_router: APIRouter = APIRouter(
    tags=["dch admin"],
    responses={404: {"description": "Not found"}},
)


@dch_admin_router.post("/test-db-connection")
def test_db_connection(
        payload: DBConnectionCreate,
        user: dict = Depends(AuthUtils.require_role("admin"))  # ✅ Superuser required
):
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

        return {"success": True, "msg": "Connection successful!"}

    except Exception as e:
        da_logger.exception(f"ERROR: {e}")
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")

@dch_admin_router.post("/add-db-connection")
def add_db_connection(
        payload: DBConnectionCreate,
        user: dict = Depends(AuthUtils.require_role("admin"))  # ✅ Superuser required
):
    try:
        da_logger.debug(f"user: {user}")
        p = payload.params
        encrypted_password = CryptoUtils().encrypt_txt(p.password)

        db_conn = DBConnection.get_db_connection(
            con_name=payload.connection_name,
            engine=payload.engine,
            host=p.host,
            port=p.port,
            user=p.user,
            password=encrypted_password,
            db_name=p.name
        )
        return {
            "success": True,
            "msg": f"Connection '{db_conn.name}' added successfully.",
            "data": {"id": db_conn.id, "name": db_conn.name}
        }
    except Exception as e:
        da_logger.exception(f"ERROR: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@dch_admin_router.get("/db_table_list/{db_id}")
async def db_table_list(
        db_id: str,
        user: dict = Depends(AuthUtils.require_role("admin"))  # ✅ Superuser required
):
    obj: DBConnection = DBConnection.query().filter_by(id=db_id).first()
    if obj is not None:
        engine = obj.get_sqlalchemy_engine()
        db_manager = GeoDBManager(engine)
        table_names = db_manager.get_spatial_table_names()
        da_logger.debug("table_name", table_names)
        return {"payload": table_names}
    return Response(status_code=204)

@dch_admin_router.get("/save_db_layer_info/{db_id}/{table_name}/{layer_category_id}/")
async def save_db_layer_info(db_id: str, table_name: str, layer_category_id: str, user: dict = Depends(AuthUtils.require_role("admin")) ):
    obj: DBConnection = DBConnection.query().filter_by(id=db_id).first()
    if obj is not None:
        engine = obj.get_sqlalchemy_engine()
        manager = GeoDBManager(engine)
        is_created = DCHUtils.create_vector_layer_info(manager, table_name, layer_category_id=layer_category_id, user=user)
        return {"payload": {"msg": "Layer info created" if is_created else "Failed to create Layer Info"}}
    return Response(status_code=204)


@dch_admin_router.post("/save_model_data/")
async def save_model_data(
        request: Request,
        model_name: str,
        user: dict = Depends(AuthUtils.require_role("admin"))  # ✅ Superuser only
):
    data = await request.json()
    pk = data.get("pk", {})
    pk_col_name = pk.get("colName")
    pk_col_value = pk.get("colValue")
    row_data = data.get("rowData", {})

    # model_class = LayerInfo
    model_class = ModelUtils.get_model(model_name)
    manager = model_class.manager()
    session = manager.get_session()

    try:
        obj = session.query(model_class).filter(getattr(model_class, pk_col_name) == pk_col_value).first()

        if obj:
            for key, value in row_data.items():
                if not hasattr(model_class, key):
                    continue

                model_field = getattr(model_class, key)
                column = model_field.property.columns[0]
                col_type_name = type(column.type).__name__

                # Convert ChoiceType label to DB value
                if hasattr(column.type, 'choices'):
                    choice_map = {label: db_value for db_value, label in column.type.choices}
                    value = choice_map.get(value, value)

                # Convert dicts to code or JSON strings
                if isinstance(value, dict):
                    if "code" in value:
                        value = value["code"]
                    elif col_type_name not in ['JSON', 'JSONB']:
                        value = json.dumps(value)

                setattr(obj, key, value)

            obj.save()
            return {"status": "success", "pk": pk, "updated": row_data}

        return {"status": "not_found", "pk": pk}, 404

    except Exception as e:
        da_logger.error(f"ERROR saving LayerInfo: {e}")
        return Response(status_code=204)

    finally:
        manager.terminate_idle_connections(session=session)
        session.close()
