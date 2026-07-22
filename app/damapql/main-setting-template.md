DAMapQL Host Settings Template

The companion settings-template.txt is a reusable starting point for theroot settings.py of any application that consumes DAMapQL. It keeps sharedDAMapQL configuration inside app.damapql while allowing WASA, LBDC, GeoCAD,or another host application to register its own modules, databases, modelbases, routers, and AOI implementation.

How to use it

Copy settings-template.txt to the project root as settings.py.

Replace project and every app.project... import path with the hostapplication's package name and paths.

Configure the required values in .env.

Add every host module to HOST_APPS.

Add every host database to APP_DATABASES.

Add extension callables to DAMAPQL_APP_EXTENSIONS.

Return every distinct host SQLAlchemy base from get_model_bases().

The resulting layout is expected to resemble:

project-root
├── .env
├── main.py
├── settings.py
└── app
    ├── damapql
    └── project
        ├── app_extension.py
        ├── models
        ├── routers
        └── utils/adapters/db.py

Why .env is loaded first

load_dotenv() runs before app.damapql.settings is imported. This orderingis important because DAMapQL settings may read values such as databasecredentials and secret keys during module import. Loading the environmentafter that import can leave DAMapQL with missing or stale values.

Paths

Setting

Purpose

BASE_DIR

Root directory containing settings.py

APP_DIR

Python application packages

MEDIA_DIR

Uploaded or generated media

COG_DIR

Cloud Optimized GeoTIFF files, when used

The application bootstrap can create writable directories such asMEDIA_DIR. Merely defining a path does not create it.

Runtime settings

Setting

Purpose

IS_LOCAL

Enables local-development behavior

IS_DOCKER

Selects Docker-aware host defaults

APP_NAME

FastAPI/OpenAPI application title

APP_VERSION

API version displayed in OpenAPI

APP_DESCRIPTION

API description displayed in OpenAPI

LOG_LEVEL

Application logging threshold

SECRET_KEY

Signing/encryption secret required at startup

The template accepts true, 1, yes, or on as true boolean values,without depending on exact capitalization.

SECRET_KEY has no unsafe default. The application fails immediately when itis missing, rather than starting with a public development secret.

Generate a suitable value with Python:

python -c "import secrets; print(secrets.token_urlsafe(48))"

CORS and map defaults

ALLOWED_ORIGINS is a comma-separated environment value:

ALLOWED_ORIGINS=http://localhost:3000,https://maps.example.com

Explicit origins are safer than *, especially when the FastAPI applicationenables cookies or credentialed requests.

DEFAULT_EXTENT contains four numbers in this order:

minimum X, minimum Y, maximum X, maximum Y

Its coordinates must use DEFAULT_SRID. A geographic project might use SRID4326; a Web Mercator project commonly uses 3857.

Application registry

HOST_APPS describes modules owned by the consuming application. Each entrycontains:

Field

Meaning

path

Importable Python package containing the module

db_key

Key used to resolve its database from DATABASES

base

Dotted path to its SQLAlchemy declarative base

Example with two modules sharing one application database and model base:

HOST_APPS = {
    "wasa": {
        "path": "app.wasa",
        "db_key": "wasa_data",
        "base": "app.wasa.utils.adapters.db.WASABase",
    },
    "survey": {
        "path": "app.survey",
        "db_key": "wasa_data",
        "base": "app.wasa.utils.adapters.db.WASABase",
    },
}

The final registry is composed as:

DA_APPS = {
    **DCH_APPS,
    **HOST_APPS,
}

Python applies values from left to right. If the same key occurs twice, thehost value wins. Duplicate keys should normally be avoided; an override shouldbe deliberate.

Model bases

get_model_bases() gives migration, schema-sync, and model-discovery code allSQLAlchemy declarative bases used by the process.

Return each distinct base only once. If several modules share ProjectBase,do not repeat it:

def get_model_bases() -> list[type]:
    from app.damapql.settings import get_dch_model_bases
    from app.project.utils.adapters.db import ProjectBase

    return [
        *get_dch_model_bases(),
        ProjectBase,
    ]

Imports are intentionally inside the function. This helps prevent circularimports while Python loads settings and initializes the application.

Application extensions

DAMAPQL_APP_EXTENSIONS is the bridge between the reusable DAMapQL bootstrapand host-specific behavior. Each value uses this format:

module.path:callable_name

For example:

DAMAPQL_APP_EXTENSIONS = [
    "app.wasa.app_extension:configure_wasa_extension",
    "app.survey.app_extension:configure_survey_extension",
]

The extension may register a concrete AOI implementation, include routers, orinstall another host-owned integration:

from fastapi import FastAPI

from app.damapql.auth.user_aoi import BaseUserAOI
from app.wasa.models.admin import UserAOI
from app.wasa.routers import wasa_router


def configure_wasa_extension(app: FastAPI) -> None:
    BaseUserAOI.register_implementation(UserAOI)
    app.include_router(wasa_router, prefix="/wasa")

This is dependency injection at the application composition boundary:DAMapQL defines the contract, while the host application supplies the concreteimplementation. DAMapQL therefore does not import app.wasa, app.lbdc, orapp.geocad directly.

Database configuration

DATABASES is a registry, not a single connection. Any number of connectionscan be added as long as every key is unique and any referenced db_keymatches it exactly.

The primary application entry uses DEFAULT_DB_KEY as its dictionary key, sochanging that environment value also changes the registered key withoutcreating a mismatch:

APP_DATABASES = {
    DEFAULT_DB_KEY: {
        # connection settings
    },
}

The template merges two groups:

DATABASES = {
    **DCH_DATABASES,
    **APP_DATABASES,
}

DCH_DATABASES contains connections owned by DAMapQL, such as auth/DCH.

APP_DATABASES contains databases owned by the host application.

Host entries are applied last, so a duplicate host key overrides a DAMapQLkey. Prefer unique keys unless replacement is explicitly intended.

Typical arrangements include:

Project design

Example keys

One database for everything

dch and app_data may point to the same server/database

Separate system and business data

dch, app_data

Separate GIS store

dch, app_data, app_gis

Several application domains

dch, cadastre_data, survey_data, imagery_data

To add another database:

APP_DATABASES["survey_data"] = {
    "engine": "postgresql",
    "host": os.getenv("SURVEY_DB_HOST", "localhost"),
    "name": os.getenv("SURVEY_DB_NAME"),
    "port": os.getenv("SURVEY_DB_PORT", "5432"),
    "user": os.getenv("SURVEY_DB_USER"),
    "password": os.getenv("SURVEY_DB_PASSWORD"),
}

Then reference the exact key from the appropriate application entry:

HOST_APPS["survey"] = {
    "path": "app.survey",
    "db_key": "survey_data",
    "base": "app.survey.utils.adapters.db.SurveyBase",
}

host.docker.internal is useful when the application runs inside Docker butPostgreSQL runs on the host. If PostgreSQL is another Compose service, use itsservice name—such as postgres—through APP_DB_HOST instead.

Middleware

DAMapQL middleware is preserved and host middleware can be appended:

MIDDLEWARES = [
    *DCH_MIDDLEWARES,
    "app.project.middlewares.audit",
]

The import path should refer to the module expected by the bootstrap. Order canmatter when middleware wraps other middleware, so add project middleware in adeliberate order.

Suggested .env

IS_LOCAL=True
IS_DOCKER=False

APP_NAME=DAMapQL Application
APP_VERSION=0.1.0
APP_DESCRIPTION=FastAPI application powered by DAMapQL
LOG_LEVEL=INFO
SECRET_KEY=replace-with-a-long-random-value

ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
DEFAULT_EXTENT=7717832.1849,3211585.4032,8389662.8759,4031923.9742
DEFAULT_SRID=3857

DEFAULT_DB_KEY=app_data
APP_DB_ENGINE=postgresql
APP_DB_HOST=localhost
APP_DB_NAME=project_data
APP_DB_PORT=5432
APP_DB_USER=postgres
APP_DB_PASSWORD=change-me

# Optional GIS database
APP_GIS_ENGINE=postgresql
APP_GIS_HOST=localhost
APP_GIS_NAME=project_gis
APP_GIS_PORT=5432
APP_GIS_USER=postgres
APP_GIS_PASSWORD=change-me

Keep real .env files outside version control. Commit only a redacted.env-template containing placeholder values.

Validation

After adapting the template, check syntax and application composition:

python -m py_compile settings.py
python -m compileall -q app/damapql
python -c "from main import app; print(app.title, len(app.routes))"

To confirm the concrete AOI provider:

python -c "from main import app; from app.damapql.auth.user_aoi import BaseUserAOI; provider = Ba