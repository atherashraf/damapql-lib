import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

# rest of your imports


BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"
MEDIA_DIR = BASE_DIR / "media"
COG_DIR = BASE_DIR / "cog"

IS_LOCAL = os.environ.get("IS_LOCAL", "True") == "True"
IS_DOCKER = os.environ.get("IS_DOCKER") == "True"
APP_NAME = os.getenv("APP_NAME", "WASA")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # e.g., "DEBUG", "ERROR"

localhost = 'host.docker.internal' if IS_DOCKER else 'localhost'

ALLOWED_ORIGINS = ["*"]

DA_APPS = {
    "auth": {"path": "app.auth", "db_key": "default"},
    "dch": {"path": "app.dch", "db_key": "default"},
}

DEFAULT_DB_KEY = 'default'

DATABASES = {
    "cache_db": {
        "engine": "sqlite",
        "file_path": os.path.join(BASE_DIR, "dch_cache.sqlite")
    },
    "default": {
        "engine": "postgresql",
        "host": os.environ.get("DCH_HOST") if not IS_LOCAL else localhost,
        "name": os.environ.get("DCH_NAME"),
        "port": os.environ.get("DCH_PORT"),
        "user": os.environ.get("DCH_USER"),
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
