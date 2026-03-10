import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

# rest of your imports


DCH_BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = DCH_BASE_DIR / "media"
COG_DIR = DCH_BASE_DIR / "cog"

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER") == "True"
APP_NAME = os.getenv("APP_NAME", "WASA")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # e.g., "DEBUG", "ERROR"

localhost = 'host.docker.internal' if IS_DOCKER else 'localhost'

ALLOWED_ORIGINS = ["*"]



DCH_DB_KEY = 'dch'
DATA_DB_KEY = 'data'

DA_APPS = {
    "auth": {"path": "app.damapql.auth", "db_key": DCH_DB_KEY},
    "dch": {"path": "app.damapql.dch", "db_key": DCH_DB_KEY},
}

DATABASES = {
    "cache_db": {
        "engine": "sqlite",
        "file_path": os.path.join(DCH_BASE_DIR, "../../dch_cache.sqlite")
    },
    "dch": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST") if not IS_LOCAL else localhost,
        "name": os.environ.get("DCH_NAME"),
        "port": os.environ.get("DCH_PORT"),
        "user": os.environ.get("DCH_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    },
    "data": {
        "engine": "postgresql",
        "host": os.environ.get("DATA_HOST") if not IS_LOCAL else localhost,
        "name": os.environ.get("DATA_NAME"),
        "port": os.environ.get("DATA_PORT"),
        "user": os.environ.get("DATA_USER"),
        "password": os.environ.get("DCH_PASSWORD")
    }

}

MIDDLEWARES = []
ACCESS_TOKEN_EXPIRE_MINUTES = 30  # 30 minutes
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
SECRET_KEY = os.getenv("SECRET_KEY")
CRYPTO_KEY = os.getenv("CRYPTO_KEY")

AWS_ACCESS_KEY_ID  = os.getenv("AWS_ACCESS_KEY_ID", None)
AWS_SECRET_ACCESS_KEY= os.getenv("AWS_SECRET_ACCESS_KEY", None)
AWS_REGION_NAME = os.getenv("AWS_DEFAULT_REGION", None)
