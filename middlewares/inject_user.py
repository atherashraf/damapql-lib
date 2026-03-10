import jwt  # <- this is PyJWT
from jwt import ExpiredSignatureError, InvalidTokenError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from mocc.app.auth.models import User
from mocc.app.auth.utils import JWT_SECRET_KEY, ALGORITHM


class InjectUserMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.user = None

        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ")[1]
            try:
                payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
                user = User.get_user_info(payload["sub"])
                request.state.user = user
            except (ExpiredSignatureError, InvalidTokenError):
                pass  # ignore invalid tokens silently

        return await call_next(request)
