# Separate dictionaries for async and sync components
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, Session

from settings import DATABASES

_async_engines = {}
_async_session_factories = {}



# --- Common Helper Method for DB URL Construction ---
def _build_db_url(db_config: dict, is_async: bool) -> str:
    """Constructs the database URL based on config and sync/async flag."""
    engine = db_config["engine"]
    prefix = ""

    if is_async:
        if engine == "postgresql":
            prefix = "postgresql+asyncpg"
        elif engine == "sqlite":
            prefix = "sqlite+aiosqlite"
        else:
            raise ValueError(f"Unsupported DB engine for async: {engine}")
    else:
        if engine == "postgresql":
            prefix = "postgresql"
        elif engine == "sqlite":
            prefix = "sqlite"
        else:
            raise ValueError(f"Unsupported DB engine for sync: {engine}")

    if engine == "postgresql":
        user = quote_plus(db_config["user"])
        password = quote_plus(db_config["password"])
        host = db_config["host"]
        port = db_config["port"]
        dbname = db_config["name"]
        return f"{prefix}://{user}:{password}@{host}:{port}/{dbname}"
    elif engine == "sqlite":
        return f"{prefix}:///{db_config['file_path']}"
    else:
        # This case should ideally be caught by the prefix logic above, but as a safeguard
        raise ValueError(f"Unsupported DB engine: {engine}")


# --- Asynchronous Database Access for Routers ---

def get_async_engine_instance(key: str = "default"):
    """Return the initialized AsyncEngine."""
    if key not in _async_engines:
        db_config = DATABASES[key]
        db_url = _build_db_url(db_config, is_async=True) # Use common method

        engine = create_async_engine(db_url, echo=False, future=True)
        _async_engines[key] = engine
        _async_session_factories[key] = sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    return _async_engines[key]

async def get_async_db_session(key: str = "default") -> AsyncSession:
    """Dependency for asynchronous FastAPI routes."""
    get_async_engine_instance(key) # Ensure engine and factory are initialized
    AsyncSessionLocal = _async_session_factories[key]
    async with AsyncSessionLocal() as db:
        yield db

# --- Synchronous Database Access for SQLAdmin ---

_sync_engines = {}
_sync_session_factories = {}

def get_sync_engine_instance(key: str = "default"):
    """Return the initialized Synchronous Engine."""
    if key not in _sync_engines:
        db_config = DATABASES[key]
        db_url = _build_db_url(db_config, is_async=False) # Use common method

        engine = create_engine(db_url, echo=False, future=True)
        _sync_engines[key] = engine
        _sync_session_factories[key] = sessionmaker(
            bind=engine, autoflush=False, autocommit=False
        )
    return _sync_engines[key]

def get_sync_db_session(key: str = "default") -> Session:
    """Dependency for synchronous components like SQLAdmin."""
    get_sync_engine_instance(key) # Ensure engine and factory are initialized
    SyncSessionLocal = _sync_session_factories[key]
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_lbdc_session():
    key="lbdc"
    get_sync_engine_instance(key)  # Ensure engine and factory are initialized
    SyncSessionLocal = _sync_session_factories[key]
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()
