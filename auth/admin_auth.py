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
        username, password = form.get("username"), form.get("password")
