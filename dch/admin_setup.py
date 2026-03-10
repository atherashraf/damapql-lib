from digitalarzengine.utils.singletons import da_logger
from sqladmin import Admin
from starlette.applications import Starlette

from app.damapql.auth.admin_auth import AdminAuth
from utils.adapters.deps import get_async_engine_instance

_dch_admin = None


def create_dch_admin(
        app: Starlette,
        secret_key: str,
        port:int,
        db_key: str = "dch",
        base_url: str = "/dch_admin",
        title: str = "DAFastMap Admin",
) -> Admin:
    global _dch_admin

    if _dch_admin is not None:
        return _dch_admin

    auth_engine = get_async_engine_instance(db_key)
    da_logger.success(f"Admin URL is running at http://127.0.0.1:{port}/api/dch_admin/")

    _dch_admin = Admin(
        app,
        auth_engine,
        base_url=base_url,
        authentication_backend=AdminAuth(
            secret_key=secret_key,
            engine=auth_engine,
        ),
        title=title,
    )
    return _dch_admin


def get_dch_admin():
    if _dch_admin is None:
        raise RuntimeError("DCH admin has not been initialized yet.")
    return _dch_admin