
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
APP_NAME=damapql

```

* `IS_LOCAL` selects development/local settings where supported.
* `IS_DOCKER` allows settings to use Docker-specific host resolution.
* `APP_FE_URL` is used for frontend links such as account activation URLs.

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

---

## 6. Email configuration

If the project sends notification or account-activation emails through Gmail, configure an application password:

```dotenv
GMAIL_PASS_CODE=

```

---

## 7. MinIO object storage

Used for storing and streaming raster layers, Cloud-Optimized GeoTIFFs (COGs), and media files:

```dotenv
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=False
MINIO_BUCKET_NAME=damapql-rasters

```

* `MINIO_ENDPOINT`: Host and port of the MinIO instance (`minio:9000` when running under Docker Compose).
* `MINIO_ACCESS_KEY`: MinIO root or IAM user access key.
* `MINIO_SECRET_KEY`: MinIO secret access key.
* `MINIO_SECURE`: Set to `True` for HTTPS/TLS connections in production, `False` for local HTTP.
* `MINIO_BUCKET_NAME`: Default bucket name used for raster layer storage.

---

## 8. Optional AWS S3 storage

If storing directly in AWS S3 instead of a self-hosted MinIO instance:

```dotenv
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION_NAME=us-east-1
AWS_STORAGE_BUCKET_NAME=

```

---

## 9. Docker considerations

Example when the backend, MinIO, and PostgreSQL run as Docker Compose services:

```dotenv
IS_DOCKER=True
DCH_HOST=postgres
DATA_HOST=postgres
APP_DATA_HOST=postgres
MINIO_ENDPOINT=minio:9000

```

-------
## 10. Spatial Defaults & Viewport Extents
Configures the default spatial coordinate system and initial bounding box fallback for newly registered layers or empty tables.
```dotenv
DEFAULT_SRID=3857
DEFAULT_EXTENT_SRID=3857
DEFAULT_EXTENT=6775796.0,2716186.0,8665099.0,4457375.0

```
---------
## 11. Security notes

* Copy `.env-template.txt` to `.env`; do not edit the distributable template with real secrets.


* Add `.env` to `.gitignore`.


* Never commit database passwords, MinIO access keys, JWT secrets, encryption keys, Gmail App Passwords, or cloud credentials.


* Use separate credentials and keys for development, staging, and production.


* Ensure every spatial PostgreSQL database has PostGIS enabled.


