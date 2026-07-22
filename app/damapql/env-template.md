# Environment Configuration Guide

DAMapQL host applications read environment variables from a project `.env` file or the deployment environment. The exact variables consumed are determined by the host project's `settings.py`.

Use the accompanying `.env-template.txt` as a copy-ready starting point. Rename or copy it to `.env`, then replace all placeholder values.

Never commit a populated `.env` file containing real credentials or secrets.

---

## 1. Runtime mode

```dotenv
IS_LOCAL=True
IS_DOCKER=False
APP_FE_URL=http://localhost:5173
```

- `IS_LOCAL` selects development/local settings where supported.
- `IS_DOCKER` allows settings to use Docker-specific host resolution.
- `APP_FE_URL` is used for frontend links such as account activation URLs.

When the API runs inside Docker but PostgreSQL runs on the host, a project may use `host.docker.internal`. When both services run in Docker Compose, use the PostgreSQL service name instead.

---

## 2. DAMapQL core database

The `DCH_*` variables configure the database used by DAMapQL authentication, roles, profiles, organizations, departments, data-catalogue tables, and related core services.

```dotenv
DCH_ENGINE=postgresql
DCH_HOST=localhost
DCH_PORT=5432
DCH_USER=postgres
DCH_PASSWORD=
DCH_NAME=damapql
```

The corresponding database key is normally:

```python
DCH_DB_KEY = "dch"
```

---

## 3. Optional shared data database

Use `DATA_*` for shared spatial or business data that is not owned by one application:

```dotenv
DATA_ENGINE=postgresql
DATA_HOST=localhost
DATA_PORT=5432
DATA_USER=postgres
DATA_PASSWORD=
DATA_NAME=shared_data
```

Use `DATA_*` consistently. Do not mix it with the older `DCH_DATA_*` naming convention.

If a separate shared database is unnecessary, `data` and `dch` may point to the same physical PostgreSQL database.

---

## 4. Application-specific databases

Each host application can define its own database prefix and database key.

Generic example:

```dotenv
APP_DATA_ENGINE=postgresql
APP_DATA_HOST=localhost
APP_DATA_PORT=5432
APP_DATA_USER=postgres
APP_DATA_PASSWORD=
APP_DATA_NAME=application_data
```

WASA example:

```dotenv
WASA_ENGINE=postgresql
WASA_HOST=localhost
WASA_PORT=5432
WASA_USER=postgres
WASA_PASSWORD=
WASA_NAME=wasa_data
```

LBDC and GeoCAD can use equivalent prefixes:

```text
LBDC_*
GEOCAD_*
```

The variable names must match those read by the host project's `settings.py`. The associated key in `DA_APPS` must exist in `DATABASES`.

Application-specific databases may be physically separate or may point to the same PostgreSQL server/database when isolation is unnecessary.

---

## 5. Security keys

```dotenv
SECRET_KEY=
CRYPTO_KEY=
JWT_SECRET_KEY=
JWT_REFRESH_SECRET_KEY=
```

Generate keys with DAMapQL's management module:

```bash
python -m app.damapql.manage generate_crypto_key
python -m app.damapql.manage generate_secret_key_hex --length 32
```

Run the secret-key command separately for `SECRET_KEY`, `JWT_SECRET_KEY`, and `JWT_REFRESH_SECRET_KEY` so every purpose receives an independent value.

For Docker:

```bash
docker exec -it <backend_container> \
    python -m app.damapql.manage generate_crypto_key

docker exec -it <backend_container> \
    python -m app.damapql.manage generate_secret_key_hex \
    --length 32
```

Use different secrets for development, staging, and production.

---

## 6. Email configuration

If the project sends notification or account-activation emails through Gmail, configure an application password:

```dotenv
GMAIL_PASS_CODE=
```

Use a Gmail App Password, not a personal account password. If the host project's email manager requires sender, SMTP host, port, or username variables, add the exact names consumed by that implementation.

---

## 7. Optional object storage

For AWS S3-compatible storage:

```dotenv
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION_NAME=us-east-1
AWS_STORAGE_BUCKET_NAME=
```

Only define these variables when the host application uses object storage. Confirm the exact region and bucket variable names expected by the storage adapter.

---

## 8. Docker considerations

Example when the backend and PostgreSQL are separate Docker Compose services:

```dotenv
IS_DOCKER=True
DCH_HOST=postgres
DATA_HOST=postgres
APP_DATA_HOST=postgres
```

Example when the backend container connects to PostgreSQL running on the host:

```dotenv
IS_DOCKER=True
DCH_HOST=host.docker.internal
DATA_HOST=host.docker.internal
APP_DATA_HOST=host.docker.internal
```

Do not use `localhost` from inside a container unless PostgreSQL is running in that same container.

---

## 9. Security notes

- Copy `.env-template.txt` to `.env`; do not edit the distributable template with real secrets.
- Add `.env` to `.gitignore`.
- Never commit database passwords, JWT secrets, encryption keys, Gmail App Passwords, or cloud credentials.
- Use separate credentials and keys for development, staging, and production.
- Prefer least-privilege database users.
- Rotate secrets when exposure is suspected.
- Use a secrets manager for production where possible.
- Ensure every spatial PostgreSQL database has PostGIS enabled.
