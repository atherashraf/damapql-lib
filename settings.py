import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


load_dotenv()

# rest of your imports


DCH_BASE_DIR = Path(__file__).resolve().parent
DCH_MEDIA_DIR = DCH_BASE_DIR / "media"
DCH_COG_DIR = DCH_BASE_DIR / "cog"

MEDIA_BASE = Path(os.getenv("MEDIA_BASE", "/media"))

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER") == "True"
DCH_APP_NAME = os.getenv("APP_NAME", "DAMapQL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # e.g., "DEBUG", "ERROR"

# Fallback SRID: 3857
try:
    DEFAULT_EXTENT_SRID = int(os.getenv("DEFAULT_EXTENT_SRID", 3857))
except (ValueError, TypeError):
    DEFAULT_EXTENT_SRID = 3857

# Default fallback extent for Pakistan in EPSG:3857: [minx, miny, maxx, maxy]
PAKISTAN_EXTENT_3857 = [6770000.0, 2690000.0, 8730000.0, 4440000.0]

def _parse_extent(raw_val: str | None) -> list[float]:
    if not raw_val:
        return PAKISTAN_EXTENT_3857
    try:
        # Handles JSON string like "[6770000, 2690000, 8730000, 4440000]"
        if raw_val.strip().startswith("["):
            parsed = json.loads(raw_val)
            if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
                return [float(v) for v in parsed]
        # Handles comma-separated string like "6770000,2690000,8730000,4440000"
        parts = [float(v.strip()) for v in raw_val.split(",") if v.strip()]
        if len(parts) == 4:
            return parts
    except Exception:
        pass
    return PAKISTAN_EXTENT_3857

DEFAULT_EXTENT = _parse_extent(os.getenv("DEFAULT_EXTENT"))

localhost = 'host.docker.internal' if IS_DOCKER else 'localhost'


def resolve_db_host(env_var_name: str) -> str:
    configured_host = os.environ.get(env_var_name)
    if configured_host:
        return configured_host
    return localhost

ALLOWED_ORIGINS = ["*"]



DCH_DB_KEY = 'dch'
DCH_DATA_DB_KEY = 'data'

DCH_APPS = {
    "auth": {"path": "app.damapql.auth", "db_key": DCH_DB_KEY},
    "dch": {"path": "app.damapql.dch", "db_key": DCH_DB_KEY},
}


def get_dch_model_bases() -> list[type]:
    """
    Return DAMapQL database model bases.

    The local import prevents a circular import while settings are loading.
    """
    from app.damapql.utils.adapters.db import DBBase
    return [DBBase]


DCH_DATABASES = {
    "stac_db":  DCH_BASE_DIR / "data" / f"stac_{DCH_APP_NAME}.duckdb",
    "cache_db": {
        "engine": "sqlite",
        "file_path": os.path.join(DCH_BASE_DIR, "../../dch_cache.sqlite")
    },
    "dch": {
        "engine": "postgresql",
        "host": resolve_db_host("DCH_HOST"),
        "name": os.environ.get("DCH_NAME"),
        "port": os.environ.get("DCH_PORT"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    },
    "data": {
        "engine": "postgresql",
        "host": resolve_db_host("DCH_DATA_HOST"),
        "name": os.environ.get("DCH_DATA_NAME"),
        "port": os.environ.get("DCH_DATA_PORT"),
        "user": os.environ.get("DCH_DATA_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    }

}

DCH_MIDDLEWARES = []
ACCESS_TOKEN_EXPIRE_MINUTES = 30  # 30 minutes
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
DCH_SECRET_KEY = os.getenv("SECRET_KEY")
DCH_CRYPTO_KEY = os.getenv("CRYPTO_KEY")

AWS_ACCESS_KEY_ID  = os.getenv("AWS_ACCESS_KEY_ID", None)
AWS_SECRET_ACCESS_KEY= os.getenv("AWS_SECRET_ACCESS_KEY", None)
AWS_REGION_NAME = os.getenv("AWS_DEFAULT_REGION", None)



# MinIO Configuration
MINIO_ENDPOINT: str = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY: str = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY: str = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE: bool = (os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "t", "yes"))

# Default bucket for ingestion
MINIO_BUCKET_NAME: Optional[str] = (os.getenv("MINIO_BUCKET_NAME") or None)
