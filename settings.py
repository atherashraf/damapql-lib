import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

# rest of your imports


DCH_BASE_DIR = Path(__file__).resolve().parent
DCH_MEDIA_DIR = DCH_BASE_DIR / "media"
DCH_COG_DIR = DCH_BASE_DIR / "cog"

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER") == "True"
DCH_APP_NAME = os.getenv("APP_NAME", "DAMapQL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # e.g., "DEBUG", "ERROR"

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
