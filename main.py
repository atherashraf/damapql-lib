import importlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, JSONResponse

from app.damapql.auth.routers import auth_router
from app.damapql.dch.routers import dch_router
from app.damapql.dch.admin_setup import create_dch_admin
from utils.adapters.deps import (
    ExternalEngineRegistry,
    terminate_all_idle_connections,
    _async_engines,
    _sync_engines,
)
from middlewares.logger_middleware import LoggingMiddleware
from utils.singletons import da_logger
from manage import manage_router
from settings import SECRET_KEY, DA_APPS, MIDDLEWARES, ALLOWED_ORIGINS, DCH_DB_KEY



@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🟢 Application startup")
    yield
    print("🔴 Application shutdown - Closing DB Pools")

    for engine in _async_engines.values():
        await engine.dispose()

    for engine in _sync_engines.values():
        engine.dispose()

    ExternalEngineRegistry.dispose_all()


app = FastAPI(
    debug=True,
    lifespan=lifespan,
    title="WASA API",
    description="API for the LBDC platform",
    version="0.1.0",
    redirect_slashes=False,
    root_path="/api"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware, log_response=False)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

port = 8998

dch_admin = create_dch_admin(
    app,
    secret_key=SECRET_KEY,
    port=port,
    db_key=DCH_DB_KEY,
)


def register_routers(app: FastAPI):
    app.include_router(manage_router, prefix="/manage")
    app.include_router(auth_router, prefix="/auth")
    app.include_router(dch_router, prefix="/dch")


def app_init():
    for middleware in MIDDLEWARES:
        importlib.import_module(middleware)

    for key, ap in DA_APPS.items():
        module_path = f'{ap["path"]}.admin'
        try:
            if importlib.util.find_spec(module_path):
                importlib.import_module(module_path)
            else:
                da_logger.debug(f"No admin module found for '{key}' at '{module_path}'")
        except Exception:
            da_logger.exception(f"Failed to load admin module for '{key}'")


app_init()
register_routers(app)


@app.exception_handler(ValueError)
async def value_error_exception_handler(request: Request, exc: ValueError):
    if "CONFLICT" in str(exc):
        return JSONResponse(
            status_code=409,
            content={"error": "ConflictError", "message": str(exc)}
        )
    return JSONResponse(
        status_code=400,
        content={"error": "BadRequest", "message": str(exc)}
    )


@app.get("/")
def read_docs():
    return RedirectResponse(url="/docs")


@app.get("/ping")
async def ping():
    da_logger.info("📡 Ping request received")
    return {"msg": "pong"}


@app.post("/admin/db/kill-idle-all")
def kill_idle_all(minutes: int = 10):
    return terminate_all_idle_connections(idle_cutoff_minutes=minutes)



if __name__ == "__main__":
    da_logger.info("🚀 FastAPI is starting...")

    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True, log_level="info")