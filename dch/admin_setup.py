from digitalarzengine.utils.singletons import da_logger
from sqladmin import Admin

from app.damapql.auth.admin_auth import BaseAdminAuth
from app.damapql.utils.adapters.deps import get_async_engine_instance

_dch_admin = None

from app.damapql.auth.models import User


class DCHAuth(BaseAdminAuth):
    def __init__(self, secret_key, engine):
        super().__init__(secret_key, engine, session_key="dch_admin_user")

    async def login(self, request):
        result = await super().login(request)

        if not result:
            return False

        form = await request.form()
        username = form.get("username")

        user = User.get_user(username)

        if not user:
            return False

        if not user.is_superuser:
            da_logger.warning(
                f"Blocked non-superuser admin login attempt: {username}"
            )

            request.session.clear()
            return False

        return True

def create_dch_admin(
    app,
    secret_key,
    port,
    db_key="dch",
    base_url="/dch_admin",
    title="DAFastMap Admin",
) -> Admin:
    global _dch_admin

    if _dch_admin is not None:
        return _dch_admin

    auth_engine = get_async_engine_instance(db_key)

    _dch_admin = Admin(
        app=app,
        engine=auth_engine,
        base_url=base_url,
        authentication_backend=DCHAuth(
            secret_key=secret_key,
            engine=auth_engine,
        ),
        title=title,
    )

    # Import locally to avoid initialization/circular-import problems.
    from app.damapql.dch.admin import (
        register_dch_admin_views,
    )

    register_dch_admin_views(_dch_admin)

    da_logger.success(
        f"Admin URL is running at "
        f"http://127.0.0.1:{port}/api{base_url}/"
    )

    return _dch_admin

def get_dch_admin():
    if _dch_admin is None:
        raise RuntimeError("DCH admin has not been initialized yet.")
    return _dch_admin



