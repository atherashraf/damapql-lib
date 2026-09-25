from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from digitalarzengine.utils.singletons import da_logger
from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import (
    HTTPException as StarletteHTTPException,
)
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from app.damapql.middlewares.logger_middleware import (
    LoggingMiddleware,
)


def get_user_facing_server_error(
    exc: BaseException,
) -> str:
    """
    Return a short client-safe message.

    Full exception details remain in server logs.
    """
    text = f"{type(exc).__name__}: {exc}"
    lower = text.lower()

    if (
        "diskfull" in lower
        or "no space left on device" in lower
        or "could not write to file" in lower
    ):
        return (
            "Database storage is full. Please free disk space "
            "on the server and try again."
        )

    if (
        "operationalerror" in lower
        or "connection refused" in lower
        or "could not connect" in lower
        or "connectionreset" in lower
        or "server closed the connection" in lower
    ):
        return (
            "Unable to reach the database. "
            "Please try again shortly."
        )

    if "timeout" in lower or "timed out" in lower:
        return "The request timed out. Please try again."

    return (
        "A server error occurred. Please try again "
        "or contact the administrator."
    )


@asynccontextmanager
async def default_lifespan(
    app: FastAPI,
) -> AsyncIterator[None]:
    da_logger.info("🟢 Application startup")

    yield

    da_logger.info("🔴 Application shutdown")


def create_damapql_app(
    *,
    title: str,
    description: str,
    version: str,
    secret_key: str,
    allowed_origins: list[str],
    media_dir: Path,
    is_local: bool,
    root_path: str = "/api",
    lifespan: Callable | None = None,
) -> FastAPI:
    """
    Create and configure the reusable DAMapQL FastAPI application.
    """
    app = FastAPI(
        debug=is_local,
        lifespan=lifespan or default_lifespan,
        title=title,
        description=description,
        version=version,
        redirect_slashes=False,
        root_path=root_path,
        swagger_ui_parameters={
            "syntaxHighlight.theme": "obsidian",
        },
    )

    configure_media(app, media_dir)
    configure_middlewares(
        app,
        secret_key=secret_key,
        allowed_origins=allowed_origins,
    )
    configure_exception_handlers(app)
    configure_system_routes(app)

    return app


def configure_media(
    app: FastAPI,
    media_dir: Path,
) -> None:
    media_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    app.mount(
        "/media",
        StaticFiles(directory=media_dir),
        name="media",
    )


def configure_middlewares(
    app: FastAPI,
    *,
    secret_key: str,
    allowed_origins: list[str],
) -> None:
    # Browsers reject `Access-Control-Allow-Origin: *` together with
    # `Access-Control-Allow-Credentials: true`. When callers allow any origin,
    # reflect the request Origin via regex so credentialed/Authorization calls work.
    origins = allowed_origins or ["*"]
    cors_kwargs: dict = {
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if origins == ["*"] or "*" in origins:
        cors_kwargs["allow_origin_regex"] = r"https?://.*"
    else:
        cors_kwargs["allow_origins"] = origins

    app.add_middleware(CORSMiddleware, **cors_kwargs)

    app.add_middleware(
        LoggingMiddleware,
        log_response=False,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
    )


def configure_exception_handlers(
    app: FastAPI,
) -> None:
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ):
        if isinstance(exc, StarletteHTTPException):
            return await http_exception_handler(
                request,
                exc,
            )

        if isinstance(exc, RequestValidationError):
            return await request_validation_exception_handler(
                request,
                exc,
            )

        try:
            da_logger.exception(
                "Unhandled error on %s %s: %s",
                request.method,
                request.url.path,
                exc,
            )
        except Exception:
            # Logging can also fail when the disk is full.
            pass

        message = get_user_facing_server_error(exc)

        return JSONResponse(
            status_code=500,
            content={
                "detail": message,
                "message": message,
            },
        )


def configure_system_routes(
    app: FastAPI,
) -> None:
    @app.get("/", include_in_schema=False)
    def read_docs():
        return RedirectResponse(url="/docs")

    @app.get(
        "/admin_be",
        include_in_schema=False,
    )
    @app.get(
        "/admin_be/",
        include_in_schema=False,
    )
    def redirect_admin():
        return RedirectResponse(
            url="/api/dch_admin/"
        )

    @app.get("/ping", tags=["system"])
    async def ping():
        da_logger.info("📡 Ping request received")
        return {"msg": "pong"}