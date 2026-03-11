from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.damapql.auth.models import User
from app.damapql.auth.utils import AuthUtils

from app.damapql.settings import DCH_DB_KEY
from app.damapql.utils.adapters.deps import get_async_engine_instance


class DCHAdminAuth(AuthenticationBackend):
    def __init__(self, secret_key: str, engine=None):
        super().__init__(secret_key)
        self.engine = engine or get_async_engine_instance(DCH_DB_KEY)

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        async with AsyncSession(self.engine) as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()

        if not user:
            return False

        if not AuthUtils.verify_password(password, user.password):
            return False

        if not user.is_superuser:
            return False

        request.session.update({"admin_user": user.id})
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        user_id = request.session.get("admin_user")
        if not user_id:
            return False

        async with AsyncSession(self.engine) as session:
            result = await session.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()

        if not user:
            return False

        return user.is_superuser