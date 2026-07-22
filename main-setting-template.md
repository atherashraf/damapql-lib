Here is the documentation cleaned up and formatted into clean, structured Markdown:

---

# DAMapQL Host Settings Template

The companion `settings-template.txt` is a reusable starting point for the root `settings.py` of any application that consumes **DAMapQL**. It keeps shared DAMapQL configuration inside `app.damapql` while allowing **WASA**, **LBDC**, **GeoCAD**, or another host application to register its own modules, databases, model bases, routers, and Area of Interest (AOI) implementation.

---

## How to Use It

1. **Copy** `settings-template.txt` to the project root as `settings.py`.
2. **Replace** `project` and every `app.project...` import path with your host application's package name and paths.
3. **Configure** the required values in `.env`.
4. **Add** every host module to `HOST_APPS`.
5. **Add** every host database to `APP_DATABASES`.
6. **Add** extension callables to `DAMAPQL_APP_EXTENSIONS`.
7. **Return** every distinct host SQLAlchemy base from `get_model_bases()`.

### Expected Directory Layout

```text
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
        └── utils
            └── adapters
                └── db.py

```

---

## Environment Initialization

### Why `.env` is Loaded First

`load_dotenv()` runs before `app.damapql.settings` is imported. This ordering is critical because DAMapQL settings may read values such as database credentials and secret keys during module import. Loading the environment *after* that import can leave DAMapQL with missing or stale values.

---

## Configuration Reference

### Paths

| Setting | Purpose |
| --- | --- |
| `BASE_DIR` | Root directory containing `settings.py` |
| `APP_DIR` | Python application packages |
| `MEDIA_DIR` | Uploaded or generated media |
| `COG_DIR` | Cloud Optimized GeoTIFF files, when used |

> **Note:** The application bootstrap can create writable directories such as `MEDIA_DIR`. Merely defining a path does not automatically create it on the filesystem.

---

### Runtime Settings

| Setting | Purpose |
| --- | --- |
| `IS_LOCAL` | Enables local-development behavior |
| `IS_DOCKER` | Selects Docker-aware host defaults |
| `APP_NAME` | FastAPI/OpenAPI application title |
| `APP_VERSION` | API version displayed in OpenAPI |
| `APP_DESCRIPTION` | API description displayed in OpenAPI |
| `LOG_LEVEL` | Application logging threshold |
| `SECRET_KEY` | Signing/encryption secret required at startup |

> **Boolean Parsing:** The template accepts `true`, `1`, `yes`, or `on` as `True` values without requiring specific capitalization.

> **Important:** `SECRET_KEY` has no unsafe default. The application fails immediately if it is missing rather than starting with a fallback development secret.

**Generate a secret key with Python:**

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"

```

---

### CORS and Map Defaults

* **`ALLOWED_ORIGINS`**: A comma-separated list of allowed origins.
```env
ALLOWED_ORIGINS=http://localhost:3000,https://maps.example.com

```


*Explicit origins are safer than `*`, especially when the FastAPI application enables cookies or credentialed requests.*
* **`DEFAULT_EXTENT`**: A sequence of four bounding numbers ordered as:

$$\text{[minimum X, minimum Y, maximum X, maximum Y]}$$


Coordinates must use `DEFAULT_SRID`. A geographic project might use SRID `4326`, while a Web Mercator project commonly uses `3857`.

---

## Application Registry

`HOST_APPS` describes modules owned by the consuming host application. Each entry accepts:

| Field | Meaning |
| --- | --- |
| `path` | Importable Python package containing the module |
| `db_key` | Key used to resolve its database from `DATABASES` |
| `base` | Dotted path to its SQLAlchemy declarative base |

### Example Configuration

Two modules sharing one application database and model base:

```python
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

```

The final registry is composed by merging dictionaries:

```python
DA_APPS = {
    **DCH_APPS,
    **HOST_APPS,
}

```

> Python evaluates dictionary unpacking from left to right. If the same key occurs twice, the host value overrides the default. Avoid duplicate keys unless an explicit override is intended.

---

## Model Bases

`get_model_bases()` provides migration, schema-sync, and model-discovery mechanisms with all SQLAlchemy declarative bases required by the process.

Return each distinct base **only once**. If several modules share `ProjectBase`, do not repeat it:

```python
def get_model_bases() -> list[type]:
    from app.damapql.settings import get_dch_model_bases
    from app.project.utils.adapters.db import ProjectBase

    return [
        *get_dch_model_bases(),
        ProjectBase,
    ]

```

> **Note:** Imports are kept inside the function to prevent circular import issues during module initialization.

---

## Application Extensions

`DAMAPQL_APP_EXTENSIONS` acts as the bridge between the reusable DAMapQL bootstrap and host-specific behavior using the format `module.path:callable_name`:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.wasa.app_extension:configure_wasa_extension",
    "app.survey.app_extension:configure_survey_extension",
]

```

An extension callable can register a concrete AOI implementation, include application routers, or install other host-owned integrations:

```python
from fastapi import FastAPI
from app.damapql.auth.user_aoi import BaseUserAOI
from app.wasa.models.admin import UserAOI
from app.wasa.routers import wasa_router

def configure_wasa_extension(app: FastAPI) -> None:
    BaseUserAOI.register_implementation(UserAOI)
    app.include_router(wasa_router, prefix="/wasa")

```

This acts as **dependency injection** at the application boundary: DAMapQL defines the interface contract while the host supplies the implementation. Consequently, DAMapQL never directly imports host modules like `app.wasa`, `app.lbdc`, or `app.geocad`.

---

## Database Configuration

`DATABASES` is a dictionary registry. Multiple connections can be configured provided every key is unique and referenced `db_key` values match.

The main application entry uses `DEFAULT_DB_KEY` as its dictionary key so changing environment variables dynamically updates registration:

```python
APP_DATABASES = {
    DEFAULT_DB_KEY: {
        # connection settings
    },
}

```

Merged databases dictionary:

```python
DATABASES = {
    **DCH_DATABASES,
    **APP_DATABASES,
}

```

* `DCH_DATABASES`: System databases owned by DAMapQL (e.g., auth/DCH).
* `APP_DATABASES`: Application databases owned by the host application.

### Common Architecture Patterns

| Project Design | Example Keys |
| --- | --- |
| **Single Database** | `dch` and `app_data` point to the same host/database |
| **Separated System & Business Data** | `dch`, `app_data` |
| **Dedicated GIS Store** | `dch`, `app_data`, `app_gis` |
| **Multi-Domain Application** | `dch`, `cadastre_data`, `survey_data`, `imagery_data` |

### Adding an Additional Database

```python
APP_DATABASES["survey_data"] = {
    "engine": "postgresql",
    "host": os.getenv("SURVEY_DB_HOST", "localhost"),
    "name": os.getenv("SURVEY_DB_NAME"),
    "port": os.getenv("SURVEY_DB_PORT", "5432"),
    "user": os.getenv("SURVEY_DB_USER"),
    "password": os.getenv("SURVEY_DB_PASSWORD"),
}

```

Then link the registry key in `HOST_APPS`:

```python
HOST_APPS["survey"] = {
    "path": "app.survey",
    "db_key": "survey_data",
    "base": "app.survey.utils.adapters.db.SurveyBase",
}

```

> **Docker Networking:** Use `host.docker.internal` if the application runs in a container but PostgreSQL runs on the host OS. If PostgreSQL is running inside a Docker Compose network, set `APP_DB_HOST` to its service name (e.g., `postgres`).

---

## Middleware

DAMapQL middleware stack is preserved while custom middleware can be appended:

```python
MIDDLEWARES = [
    *DCH_MIDDLEWARES,
    "app.project.middlewares.audit",
]

```

---

## Suggested `.env` Template

```env
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

```

> **Security Note:** Keep `.env` files out of version control. Commit only a redacted `.env-template` containing placeholder values.

---

## Validation & Testing

After configuring your settings, verify syntax and overall composition:

```bash
python -m py_compile settings.py
python -m compileall -q app/damapql
python -c "from main import app; print(app.title, len(app.routes))"

```

To verify that the concrete Area of Interest (AOI) provider has been correctly registered:

```bash
python -c "from main import app; from app.damapql.auth.user_aoi import BaseUserAOI; provider = BaseUserAOI.get_implementation(); print(provider)"

```