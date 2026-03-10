import time
import json
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


SENSITIVE_KEYS = {"password", "token", "access", "refresh"}


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, log_response: bool = False):
        super().__init__(app)
        self.log_response = log_response
        from digitalarzengine.utils.loggers import DALogger
        self.logger = DALogger(name="LoggingMiddleware")

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        request_body = await request.body()

        # Securely parse and mask sensitive fields
        body_text = ""
        try:
            parsed_body = json.loads(request_body)
            filtered_body = {
                k: ("*****" if k.lower() in SENSITIVE_KEYS else v)
                for k, v in parsed_body.items()
            }
            body_text = json.dumps(filtered_body)
        except Exception:
            body_text = request_body[:200].decode("utf-8", errors="ignore") + "..."

        self.logger.info(f"➡️  {request.method} {request.url.path} | Body: {body_text}")

        try:
            response = await call_next(request)
        except Exception as e:
            self.logger.error(f"❌ Error during request: {str(e)}")
            raise

        process_time = (time.time() - start_time) * 1000
        status_code = response.status_code

        if status_code == 200:
            self.logger.success(
                f"✅ {request.method} {request.url.path} | "
                f"Status: {status_code} | Time: {process_time:.2f}ms"
            )
        elif 200 < status_code < 500:
            self.logger.warning(
                f"⚠️  {request.method} {request.url.path} | "
                f"Status: {status_code} | Time: {process_time:.2f}ms"
            )
        else:
            self.logger.error(
                f"❌ {request.method} {request.url.path} | "
                f"Status: {status_code} | Time: {process_time:.2f}ms"
            )

        if self.log_response:
            response_body = b""
            async for chunk in response.body_iterator:
                response_body += chunk
            self.logger.debug(f"Response Body: {response_body.decode('utf-8', errors='ignore')}")
            response.body_iterator = iter([response_body])

        return response
