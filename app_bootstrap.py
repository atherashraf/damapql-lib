from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from digitalarzengine.utils.singletons import da_logger
from fastapi import FastAPI

from app.damapql.application import create_damapql_app
from app.damapql.auth.routers import auth_router
from app.damapql.dch.admin_setup import create_dch_admin
from app.damapql.dch.routers import dch_router
from app.damapql.settings import (
    DCH_DB_KEY,
    DCH_SECRET_KEY,
)
from app.lbdc.routers import lbdc_router


AppExtension = Callable[[FastAPI], None]


def import_object(import_path: str) -> Any:
    """
    Import an object using:

        package.module:object_name
    """
    try:
        module_path, object_name = import_path.split(
            ":",
            maxsplit=1,
        )
    except ValueError as exc:
        raise ValueError(
            "Import path must use the format "
            "'package.module:object_name'. "
            f"Received: {import_path!r}"
        ) from exc

    module = importlib.import_module(module_path)

    try:
        return getattr(module, object_name)
    except AttributeError as exc:
        raise ImportError(
            f"Object {object_name!r} was not found "
            f"in module {module_path!r}."
        ) from exc


def configure_extensions(
    app: FastAPI,
    extension_paths: list[str],
) -> None:
    """
    Load and execute application-specific extension functions.
    """
    for extension_path in extension_paths:
        da_logger.info(
            f"Loading application extension: "
            f"{extension_path}"
        )

        extension = import_object(extension_path)

        if not callable(extension):
            raise TypeError(
                f"Application extension is not callable: "
                f"{extension_path}"
            )

        extension(app)


# app/core/model_loader.py or directly in create_app file


def load_all_app_models() -> None:
    """
    Dynamically imports the 'models' module for every registered app in DA_APPS.
    Triggers each app's `models/__init__.py` to prime SQLAlchemy's ORM registry.
    """
    from settings import DA_APPS  # Your dict containing app paths

    for app_label, app_config in DA_APPS.items():
        app_path = app_config.get("path")
        if not app_path:
            continue

        models_module_path = f"{app_path}.models"

        try:
            importlib.import_module(models_module_path)
            da_logger.info(f"Loaded models for app: '{app_label}' ({models_module_path})")
        except ModuleNotFoundError as e:
            # Handle case where an app simply doesn't have a models package/file
            if f"No module named '{models_module_path}'" in str(e):
                da_logger.debug(f"App '{app_label}' has no models module to load.")
            else:
                da_logger.error(f"Failed to load models for app '{app_label}': {e}")
        except Exception as e:
            da_logger.exception(f"Error initializing models for app '{app_label}': {e}")

def configure_core_routers(app: FastAPI) -> None:
    app.include_router(
        auth_router,
        prefix="/auth",
    )

    app.include_router(
        dch_router,
        prefix="/dch",
    )

    app.include_router(
        lbdc_router,
        prefix="/lbdc",
    )


def create_app(port, title, description) -> FastAPI:
    from settings import (
        SECRET_KEY,
        ALLOWED_ORIGINS,
        MEDIA_DIR,
        IS_LOCAL,
        DAMAPQL_APP_EXTENSIONS,
    )

    app = create_damapql_app(
        title=title,
        description=description,
        version="0.1.0",
        secret_key=SECRET_KEY,
        allowed_origins=ALLOWED_ORIGINS,
        media_dir=MEDIA_DIR,
        is_local=IS_LOCAL,
        root_path="/api",
    )

    # 🌟 1. Load all application ORM models upfront to populate SQLAlchemy's registry
    load_all_app_models()

    # 2. First create the SQLAdmin instance.
    dch_admin = create_dch_admin(
        app,
        secret_key=DCH_SECRET_KEY,
        port=port,
        db_key=DCH_DB_KEY,
        base_url="/dch_admin",
    )

    # 3. Then explicitly register the core admin views.
    from app.damapql.auth.admin_views import (
        register_auth_admin_views,
    )

    register_auth_admin_views(dch_admin)

    # 4. Configure core routers & app extensions
    configure_core_routers(app)

    configure_extensions(
        app,
        DAMAPQL_APP_EXTENSIONS,
    )

    da_logger.info(
        "FastAPI application configuration completed."
    )
    da_logger.success("Server started successfully.")
    da_logger.info(
        f"Uvicorn running on http://127.0.0.1:{port} "
        f"(Press CTRL+C to quit)"
    )

    return app