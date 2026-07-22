# DAMapQL

Reusable **DAMap Query Layer** for **FastAPI applications**.

DAMapQL provides shared authentication, database, administration, data-catalogue, map-service, middleware, and application-bootstrap functionality for multiple DAMap-based projects. It is maintained as a standalone library and can be integrated into host applications such as WASA, LBDC, and GeoCAD.

---

## Installation

DAMapQL can be installed as a cloned repository or as a Git subtree. Choose only one method for a project.

### Option 1: Simple clone

Run from the FastAPI project root:

```bash
git clone https://github.com/atherashraf/damapql-lib.git app/damapql
```

Update it later with:

```bash
cd app/damapql
git pull origin main
```

Because this creates a nested Git repository, exclude it from the parent application repository by adding this entry to the parent `.gitignore`:

```gitignore
app/damapql/
```

When the host application is cloned on another machine, DAMapQL must be cloned separately using the same command.

### Option 2: Git subtree

This option includes DAMapQL files in the host application's repository while retaining the ability to pull and push changes to the standalone library.

Run from the FastAPI project root:

```bash
git remote add damapql-lib https://github.com/atherashraf/damapql-lib.git
git fetch damapql-lib
git subtree add --prefix=app/damapql damapql-lib main --squash
```

Pull future DAMapQL updates:

```bash
git subtree pull --prefix=app/damapql damapql-lib main --squash
```

Push committed DAMapQL changes back to the library:

```bash
git subtree push --prefix=app/damapql damapql-lib main
```

After another developer clones the host application, the subtree files are already present. The developer only needs to configure the remote:

```bash
git remote add damapql-lib https://github.com/atherashraf/damapql-lib.git
git fetch damapql-lib
```

Do not run `git subtree add` again in that clone.

### Resulting structure

Both installation methods produce the same Python import structure:

```text
project-root
├── main.py
├── settings.py
└── app
    ├── app_extension.py
    ├── api
    ├── damapql
    │   ├── application.py
    │   ├── app_bootstrap.py
    │   ├── auth
    │   ├── dch
    │   └── tests
    ├── survey
    └── wasa
```

Example imports:

```python
from app.damapql.auth import models
from app.damapql.dch import routers
```

The subtree option is preferable when DAMapQL should be versioned and distributed as part of the host application.

---

## Application bootstrap and external extensions

DAMapQL provides a reusable application factory through:

```text
app/damapql/app_bootstrap.py
```

The bootstrap is the application's **composition root**. It creates the FastAPI application, initializes DAMapQL services, and loads project-specific extensions without making DAMapQL directly dependent on WASA, LBDC, GeoCAD, Survey, or another host application.

This follows the Dependency Inversion Principle:

- DAMapQL defines reusable abstractions and services.
- The host application supplies concrete implementations.
- Application extensions inject those implementations into DAMapQL.
- DAMapQL never needs to import a particular host application directly.

### Minimal `main.py`

The host application's `main.py` only creates and exposes the configured FastAPI application:

```python
from app.damapql.app_bootstrap import create_app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8998,
        reload=False,
    )
```

The application can also be started from the command line:

```bash
uvicorn main:app --host 0.0.0.0 --port 8998
```

### Application extensions

An application extension can register:

- application-specific routers;
- concrete implementations of DAMapQL abstractions;
- middleware;
- admin views;
- exception handlers;
- startup and shutdown hooks;
- background services.

Example `app/app_extension.py`:

```python
from fastapi import FastAPI

from app.damapql.auth.user_aoi import BaseUserAOI
from app.survey import survey_router
from app.wasa.models.admin import UserAOI
from app.wasa.routers import wasa_router


def configure_wasa_extension(app: FastAPI) -> None:
    """Register WASA implementations and routers."""

    BaseUserAOI.register_implementation(UserAOI)

    app.include_router(
        wasa_router,
        prefix="/wasa",
    )


def configure_survey_extension(app: FastAPI) -> None:
    """Register Survey routers."""

    app.include_router(survey_router)
```

Register the extension entry points in the host project's `settings.py`:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.app_extension:configure_wasa_extension",
    "app.app_extension:configure_survey_extension",
]
```

Each entry uses this format:

```text
python.module.path:callable_name
```

During application creation, DAMapQL dynamically imports and executes every configured callable.

The initialization sequence is:

```text
main.py
    → create_app()
    → create_damapql_app()
    → configure_extensions()
    → configure_wasa_extension()
    → register UserAOI
    → include application routers
```

### Separate extension modules

Larger projects may keep each extension in its application folder:

```text
app
├── damapql
│   └── app_bootstrap.py
├── wasa
│   └── app_extension.py
└── survey
    └── app_extension.py
```

The settings then become:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.wasa.app_extension:configure_wasa_extension",
    "app.survey.app_extension:configure_survey_extension",
]
```

For an LBDC project:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.lbdc.app_extension:configure_lbdc_extension",
]
```

For a GeoCAD project:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.geocad.app_extension:configure_geocad_extension",
]
```

The reusable bootstrap remains inside the DAMapQL Git subtree. Each host project supplies only its own extension module and settings entries.

### Registering an external `UserAOI` implementation

DAMapQL defines the application-independent `BaseUserAOI` contract. A host application implements the contract using its own declarative database base:

```python
from app.damapql.auth.user_aoi import BaseUserAOI
from app.wasa.utils.adapters.db import WASABase


class UserAOI(BaseUserAOI, WASABase):
    __tablename__ = "user_aoi"

    # WASA-specific fields and implementation
```

The extension injects the concrete class:

```python
BaseUserAOI.register_implementation(UserAOI)
```

This is Python-level polymorphism rather than SQLAlchemy joined-table inheritance. It allows DAMapQL auth models and application AOI models to use different declarative bases, metadata collections, database sessions, or physical databases.

---

## Module overview

Current DAMapQL modules:

```text
damapql
├── application.py      → reusable FastAPI application configuration
├── app_bootstrap.py    → application factory and extension loader
├── auth                → authentication and user management
├── dch                 → data catalogue and map services
├── middlewares         → reusable middleware
└── tests               → DAMapQL test suite
```

---

## Required host settings

The host application must provide the settings imported by the DAMapQL application factory.

```python
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "replace-with-a-secure-application-secret"
IS_LOCAL = True
MEDIA_DIR = BASE_DIR / "media"

ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

DCH_DB_KEY = "dch"
DATA_DB_KEY = "data"
WASA_DB_KEY = "wasa_data"

DA_APPS = {
    "auth": {
        "path": "app.damapql.auth",
        "db_key": DCH_DB_KEY,
    },
    "dch": {
        "path": "app.damapql.dch",
        "db_key": DCH_DB_KEY,
    },
    "wasa": {
        "path": "app.wasa",
        "db_key": WASA_DB_KEY,
    },
}

DAMAPQL_APP_EXTENSIONS = [
    "app.app_extension:configure_wasa_extension",
    "app.app_extension:configure_survey_extension",
]
```

Use environment variables or a secrets manager for production secrets. Do not commit real passwords or secret keys.

---

## Database configuration

DAMapQL supports multiple named database connections. A project can use:

- a DAMapQL core database for authentication and the data catalogue;
- a shared data database for common application data;
- one or more app-specific databases such as `wasa_data`, `lbdc_data`, or `geocad_data`;
- an optional SQLite cache database.

The database key assigned to an application in `DA_APPS` must exist in `DATABASES`.

### Example multi-database configuration

```python
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER", "False") == "True"

LOCAL_DATABASE_HOST = (
    "host.docker.internal"
    if IS_DOCKER
    else "localhost"
)


def database_host(environment_name: str) -> str:
    """Use the configured host in production and the local host in development."""

    if IS_LOCAL:
        return LOCAL_DATABASE_HOST

    value = os.environ.get(environment_name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {environment_name}"
        )

    return value


DCH_DB_KEY = "dch"
DATA_DB_KEY = "data"
WASA_DB_KEY = "wasa_data"


DATABASES = {
    "cache_db": {
        "engine": "sqlite",
        "file_path": str(BASE_DIR / "dch_cache.sqlite"),
    },

    # DAMapQL authentication, roles, profiles, organizations,
    # departments, data catalogue, and related core tables.
    DCH_DB_KEY: {
        "engine": "postgresql",
        "host": database_host("DCH_HOST"),
        "name": os.environ.get("DCH_NAME"),
        "port": os.environ.get("DCH_PORT", "5432"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD"),
    },

    # Optional shared spatial or business-data database.
    DATA_DB_KEY: {
        "engine": "postgresql",
        "host": database_host("DATA_HOST"),
        "name": os.environ.get("DATA_NAME"),
        "port": os.environ.get("DATA_PORT", "5432"),
        "user": os.environ.get("DATA_USER"),
        "password": os.environ.get("DATA_PASSWORD"),
    },

    # WASA-specific operational and spatial data, including
    # the concrete UserAOI table and WASA boundary models.
    WASA_DB_KEY: {
        "engine": "postgresql",
        "host": database_host("WASA_HOST"),
        "name": os.environ.get("WASA_NAME"),
        "port": os.environ.get("WASA_PORT", "5432"),
        "user": os.environ.get("WASA_USER"),
        "password": os.environ.get("WASA_PASSWORD"),
    },
}
```

Corresponding environment variables may look like:

```dotenv
IS_LOCAL=False
IS_DOCKER=False

DCH_HOST=db.example.internal
DCH_NAME=damapql
DCH_PORT=5432
DCH_USER=damapql_user
DCH_PASSWORD=change-me

DATA_HOST=db.example.internal
DATA_NAME=shared_spatial_data
DATA_PORT=5432
DATA_USER=data_user
DATA_PASSWORD=change-me

WASA_HOST=db.example.internal
WASA_NAME=wasa
WASA_PORT=5432
WASA_USER=wasa_user
WASA_PASSWORD=change-me
```

### Adding another app-specific database

Define a database key:

```python
LBDC_DB_KEY = "lbdc_data"
```

Add its connection:

```python
DATABASES[LBDC_DB_KEY] = {
    "engine": "postgresql",
    "host": database_host("LBDC_HOST"),
    "name": os.environ.get("LBDC_NAME"),
    "port": os.environ.get("LBDC_PORT", "5432"),
    "user": os.environ.get("LBDC_USER"),
    "password": os.environ.get("LBDC_PASSWORD"),
}
```

Register the application against that key:

```python
DA_APPS["lbdc"] = {
    "path": "app.lbdc",
    "db_key": LBDC_DB_KEY,
}
```

Then add its application extension:

```python
DAMAPQL_APP_EXTENSIONS.append(
    "app.lbdc.app_extension:configure_lbdc_extension"
)
```

The same pattern applies to GeoCAD:

```python
GEOCAD_DB_KEY = "geocad_data"
```

### Database design notes

- `dch` normally contains DAMapQL auth and data-catalogue tables.
- `data` is optional and can contain shared spatial or operational data.
- App-specific keys such as `wasa_data` isolate application models and migrations.
- Different keys may point to different physical databases.
- Different keys may also point to the same physical PostgreSQL database when separation is not required.
- A concrete application model should use the declarative base configured for its own database key.
- A database key referenced by `DA_APPS` must exactly match a key in `DATABASES`.
- Avoid hard-coded database credentials; use environment variables or a secrets manager.
- PostgreSQL databases containing spatial models must have the PostGIS extension enabled.

For example, DAMapQL auth can use `DBBase` against `dch`, while WASA models use `WASABase` against `wasa_data`. The registered `BaseUserAOI` contract allows these models to collaborate without requiring cross-database SQLAlchemy inheritance.

---

## Admin initialization

When the application is created through DAMapQL's application factory, the SQLAdmin interface is initialized automatically:

```python
from app.damapql.app_bootstrap import create_app


app = create_app()
```

The host application's `main.py` does not need to call `create_dch_admin()` directly. The DAMapQL bootstrap performs the equivalent configuration internally:

```python
create_dch_admin(
    app,
    secret_key=DCH_SECRET_KEY,
    port=8998,
    db_key=DCH_DB_KEY,
    base_url="/dch_admin",
)
```

After starting the server, the admin panel is available at:

```text
http://127.0.0.1:8998/api/dch_admin/
```

Application-specific admin modules can be loaded through application extensions or the configured `DA_APPS` mechanism.

---

## Running tests

DAMapQL includes unit tests written with **pytest** under:

```text
app/damapql/tests
```

Example structure:

```text
app/damapql/tests
├── auth
│   └── test_*.py
└── dch
    └── test_*.py
```

Run authentication tests:

```bash
pytest -v app/damapql/tests/auth
```

Run DCH tests:

```bash
pytest -v app/damapql/tests/dch
```

Run all DAMapQL tests:

```bash
pytest -v app/damapql/tests
```

The `-v` option enables verbose test output.

---

## Development workflow

This repository is maintained from the primary DAMapQL development project.

### Publish subtree changes to the standalone library

Run from the host project root:

```bash
git subtree push --prefix=app/damapql damapql-lib main
```

### Pull library changes into a subtree project

Optionally configure a merge-message editor once:

```bash
git config --global core.editor "'C:/Program Files/Notepad++/notepad++.exe' -multiInst -nosession"
```

Alternatively:

```bash
git config --global core.editor notepad
```

Pull the latest library changes:

```bash
git subtree pull --prefix=app/damapql damapql-lib main --squash
```

If Git needs its index refreshed before the pull:

```bash
git update-index --refresh
git diff-index --quiet HEAD --
git fetch damapql-lib
```

Then retry the subtree pull.

### Update a simple-clone installation

For projects using the simple-clone method:

```bash
cd app/damapql
git pull origin main
```

Do not use this `cd app/damapql && git pull` workflow for a subtree installation. Use `git subtree pull` from the host project root instead.

---

# test your  code
```text
python -m compileall -q app/damapql
python -c "from main import app; from app.damapql.auth.user_aoi import BaseUserAOI; implementation = BaseUserAOI.get_implementation(); print('App:', app.title); print('UserAOI implementation:', f'{implementation.__module__}.{implementation.__name__}'); print('Routes:', len(app.routes))"
pytest -v app/damapql/tests/auth
pytest -v app/damapql/tests/dch
```
-----
## Author

**Ather Ashraf**  
AI • GIS • Software Engineering

Email: [atherashraf@gmail.com](mailto:atherashraf@gmail.com)  
Pakistan: +92 322 4785104  
Saudi Arabia: +966 55 725 2342

