# DAMapQL Project Initialization Guide

This guide describes how to initialize a new FastAPI project that uses DAMapQL. It covers Docker and local execution, core and application-specific databases, schema creation, fixtures, access-control records, application extensions, and optional maintenance tasks.

Project-specific commandsâ€”such as WASA dummy-data loadersâ€”should be documented in the host application. They should not be required by DAMapQL itself.

---

## 1. Prerequisites

Before initializing the database, confirm that the project has:

- DAMapQL installed under `app/damapql`;
- a configured `settings.py`;
- a `.env` file containing the required secrets and database connections;
- the required PostgreSQL databases and PostGIS extensions;
- the backend container running, if Docker is used;
- application modules registered in `DA_APPS`;
- application extension callables registered in `DAMAPQL_APP_EXTENSIONS`.

The recommended entry point is:

```python
from app.damapql.app_bootstrap import create_app


app = create_app()
```

---

## 2. Choose an execution mode

### Docker

Commands in this guide use a replaceable container name:

```bash
BACKEND_CONTAINER=your_backend_container
```

For WASA, for example:

```bash
BACKEND_CONTAINER=wasa_be
```

Run a DAMapQL command with:

```bash
docker exec -it "$BACKEND_CONTAINER" python -m app.damapql.manage <command>
```

In CI/CD, omit `-it`:

```bash
docker exec "$BACKEND_CONTAINER" python -m app.damapql.manage <command>
```

### Local Python environment

When the application runs outside Docker:

```bash
python -m app.damapql.manage <command>
```

Use one execution mode consistently during initialization.

---

## 3. Configure applications and extensions

Register DAMapQL and project applications in `settings.py`:

```python
DCH_DB_KEY = "dch"
APP_DATA_DB_KEY = "app_data"

DA_APPS = {
    "auth": {
        "path": "app.damapql.auth",
        "db_key": DCH_DB_KEY,
    },
    "dch": {
        "path": "app.damapql.dch",
        "db_key": DCH_DB_KEY,
    },
    "my_app": {
        "path": "app.my_app",
        "db_key": APP_DATA_DB_KEY,
    },
}

DAMAPQL_APP_EXTENSIONS = [
    "app.my_app.app_extension:configure_my_app_extension",
]
```

An extension can register routers and concrete implementations:

```python
from fastapi import FastAPI

from app.damapql.auth.user_aoi import BaseUserAOI
from app.my_app.models.admin import UserAOI
from app.my_app.routers import my_app_router


def configure_my_app_extension(app: FastAPI) -> None:
    BaseUserAOI.register_implementation(UserAOI)

    app.include_router(
        my_app_router,
        prefix="/my-app",
    )
```

Replace `my_app`, `APP_DATA_DB_KEY`, and the import paths with the current project's values.

---

## 4. Generate security keys

Generate each required secret independently:

```bash
python -m app.damapql.manage generate_crypto_key
python -m app.damapql.manage generate_secret_key_hex --length 32
```

Run the secret-key command separately for:

- `SECRET_KEY`;
- `JWT_SECRET_KEY`;
- `JWT_REFRESH_SECRET_KEY`.

Copy the generated values into `.env`. Do not reuse production secrets in development or commit `.env` to Git.

For Docker, prefix each command with:

```bash
docker exec -it "$BACKEND_CONTAINER"
```

---

## 5. Create configured databases

Create the databases configured for the project:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage create_dch_databases
```

The exact databases created depend on the project's `DATABASES` configuration. Confirm that spatial databases have PostGIS enabled.

---

## 6. Create database schemas

Migrate DAMapQL core applications first:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage migrate --app_label auth

docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage migrate --app_label dch
```

Then migrate every registered project application:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage migrate --app_label <app_label>
```

Example:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage migrate --app_label my_app
```

If Survey is enabled:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage migrate --app_label survey
```

Use `migrate` for initial table creation. Use `sync_schema` later only for supported additive changes.

---

## 7. Register application data connections

If the DCH catalogue needs a stored connection for an application database, register its configured database key:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage add_db_connection \
    --db_key <app_database_key>
```

Example:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage add_db_connection \
    --db_key app_data
```

The supplied key must exist in `DATABASES`. This command is only required when the application database should be represented in the DCH connection catalogue.

---

## 8. Load core fixtures

Load DAMapQL DCH fixtures:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage loaddata \
    --app_label dch \
    --file app/damapql/dch/fixtures/layercategory.json

docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage loaddata \
    --app_label dch \
    --file app/damapql/dch/fixtures/layerqualitylevel.json
```

---

## 9. Load application fixtures

Load application-specific fixtures after the application's schema exists:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage loaddata \
    --app_label <app_label> \
    --file app/<app_label>/fixtures/<fixture_file>.json
```

Repeat the command for each required fixture. Fixtures must be loaded in dependency order; parent/reference records should be loaded before records that depend on them.

Application fixture names belong in the host project's setup notes or command template rather than DAMapQL's generic initialization sequence.

---

## 10. Create the superuser

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage createsuperuser
```

The command prompts for the administrator's identity and credentials.

---

## 11. Configure roles and organizational structure

Create the application manager role:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage create_role \
    --name app_manager \
    --description "Manage application users and configuration"
```

Create an organization:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage create_organization \
    --name "Example Organization"
```

Create a department:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage create_department \
    --organization_name "Example Organization" \
    --name "Application Team" \
    --description "Core application team"
```

Organization and department describe the user's organizational placement. They are separate from spatial access such as district, division, subdivision, tehsil, UC, or mauza AOI assignments.

---

## 12. Create an application user

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage create_app_user \
    --role_name app_manager \
    --organization_name "Example Organization" \
    --department_name "Application Team"
```

Assign application-specific AOI information through the host application's user-management API or administrative workflow after the user exists.

---

## 13. Verify the installation

Start or restart the backend, then verify:

```text
GET /api/ping
GET /api/docs
GET /api/dch_admin/
```

Also verify that:

- login returns a valid token and `UserInfo`;
- core and application routers appear in OpenAPI;
- the registered AOI implementation is reported in startup logs;
- the DCH database connection is available;
- application fixtures are present.

Run the DAMapQL test suite:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    pytest -v app/damapql/tests
```

---

## 14. Optional application data and maintenance

Dummy-data scripts are project-specific and should run only in development or test environments:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.scripts.<dummy_data_module> \
    <arguments>
```

Add missing columns supported by DAMapQL's additive schema synchronization:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage sync_schema \
    --app_label <app_label>
```

Synchronize one model when supported:

```bash
docker exec -it "$BACKEND_CONTAINER" \
    python -m app.damapql.manage sync_schema \
    --model_name <ModelName>
```

Other maintenance commands, such as layer-accessibility synchronization or layer reconciliation, should be documented by the application that owns those tables and workflows.

> `sync_schema` is intended for supported additive changes. Use a proper migration workflow for destructive changes, column renames, type conversions, or complex data migrations.

---

## 15. Recommended initialization order

1. Configure `.env`, `DATABASES`, `DA_APPS`, and `DAMAPQL_APP_EXTENSIONS`.
2. Start the backend container.
3. Run `create_dch_databases`.
4. Migrate `auth` and `dch`.
5. Migrate every project application.
6. Register required DCH database connections.
7. Load core DCH fixtures.
8. Load application fixtures in dependency order.
9. Create the superuser.
10. Create roles, organizations, and departments.
11. Create application users.
12. Assign application-specific AOIs.
13. Verify routes, login, admin, data connections, and tests.
14. Run optional dummy-data or maintenance commands only when needed.

---

## Quick reference

| Command | Purpose |
| --- | --- |
| `create_dch_databases` | Create configured project databases |
| `migrate --app_label <app>` | Create tables for an application |
| `sync_schema --app_label <app>` | Add supported missing columns |
| `add_db_connection --db_key <key>` | Register a database in the DCH catalogue |
| `loaddata --app_label <app> --file <file>` | Load JSON fixture data |
| `dumpdata --app_label <app> --model <model> --output <file>` | Export model data |
| `createsuperuser` | Create an administrative user |
| `create_role --name <role>` | Create an application role |
| `create_organization --name <name>` | Create an organization |
| `create_department ...` | Create a department |
| `create_app_user ...` | Create a regular application user |
| `generate_crypto_key` | Generate `CRYPTO_KEY` |
| `generate_secret_key_hex --length 32` | Generate an application or JWT secret |
