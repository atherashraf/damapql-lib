from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.damapql.auth.models import User
from app.damapql.auth.utils import AuthUtils


class BaseAdminAuth(AuthenticationBackend):
    def __init__(self, secret_key: str, engine, session_key: str):
        super().__init__(secret_key)
        self.engine = engine
        self.session_key = session_key

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")

        if not username or not password:
            return False

        async with AsyncSession(self.engine) as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()

        if not user:
            return False

        if not AuthUtils.verify_password(password, user.password):
            return False

        request.session[self.session_key] = str(user.id)
        return True

    async def logout(self, request: Request) -> bool:
        request.session.pop(self.session_key, None)
        return True

    async def authenticate(self, request: Request) -> bool:
        user_id = request.session.get(self.session_key)
        if not user_id:
            return False
        return True