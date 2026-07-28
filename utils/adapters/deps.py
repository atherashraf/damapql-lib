"""
Database Dependency Module (deps.py)
-----------------------------------
Handles dynamic routing to multiple databases based on URL path.
Supports both Async (FastAPI) and Sync (SQLAdmin/Scripts) operations.

USAGE EXAMPLES:

1. Inside a FastAPI Router (Async):
    @router.get("/users")
    async def read_users(db: AsyncSession = Depends(get_async_db)):
        result = await db.execute(select(User))
        return result.scalars().all()

2. Inside a FastAPI Router (Sync):
    @router.get("/items")
    def read_items(db: Session = Depends(get_db)):
        return db.query(Item).all()

3. Standalone Script / Background Task (Sync):
    from deps import sync_session_scope

    with sync_session_scope(key=DEFAULT_DB_KEY) as db:
        user = db.query(User).first()
        # session is automatically closed/rolled back after block

4. Standalone Script (Async):
    from deps import get_async_session

    async def my_task():
        async for db in get_async_session(key="secondary"):
            result = await db.execute(select(User))
            # ...
"""

from __future__ import annotations

from digitalarzengine.utils.singletons import da_logger
from fastapi import Request
from typing import Generator, AsyncGenerator
from contextlib import contextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy import text
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus
from typing import Dict
import os

from app.damapql.utils.crypto import CryptoUtils
from app.damapql.settings import IS_LOCAL

# Expected structure from your digitalarzengine_settings.py
from app.damapql.settings import DCH_APPS, DCH_DB_KEY
from settings import DATABASES

# --- Internal Registry for Connection Pooling ---
_async_engines: dict[str, Any] = {}
_async_session_factories: dict[str, sessionmaker] = {}

_sync_engines: dict[str, Any] = {}
_sync_session_factories: dict[str, sessionmaker] = {}


# --- URL Builder ---
def _build_db_url(db_config: dict, is_async: bool) -> str:
    """Constructs a database URL based on config and driver type."""
    engine_type = db_config["engine"]

    if is_async:
        prefixes = {"postgresql": "postgresql+asyncpg", "sqlite": "sqlite+aiosqlite"}
    else:
        prefixes = {"postgresql": "postgresql", "sqlite": "sqlite"}

    if engine_type not in prefixes:
        raise ValueError(f"Unsupported DB engine: {engine_type}")

    prefix = prefixes[engine_type]

    if engine_type == "postgresql":
        user = quote_plus(db_config["user"])
        password = quote_plus(db_config["password"])
        host = db_config["host"]
        port = db_config["port"]
        dbname = db_config["name"]
        return f"{prefix}://{user}:{password}@{host}:{port}/{dbname}"

    if engine_type == "sqlite":
        return f"{prefix}:///{db_config['file_path']}"

    raise ValueError(f"Invalid configuration for {engine_type}")


# --- Engine & Factory Accessors ---
def get_async_engine_instance(key: str = DCH_DB_KEY):
    if key not in _async_engines:
        db_config = DATABASES[key]
        db_url = _build_db_url(db_config, is_async=True)
        engine = create_async_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True
        )
        _async_engines[key] = engine
        _async_session_factories[key] = sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
    return _async_engines[key]


def get_sync_engine_instance(key: str = DCH_DB_KEY):
    if key not in _sync_engines:
        db_config = DATABASES[key]
        db_url = _build_db_url(db_config, is_async=False)
        engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True
        )
        _sync_engines[key] = engine
        _sync_session_factories[key] = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _sync_engines[key]


# --- Core Session Generators ---
async def get_async_session(key: str = DCH_DB_KEY) -> AsyncGenerator[AsyncSession, None]:
    get_async_engine_instance(key)
    factory = _async_session_factories[key]
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

def get_sync_session(key: str = DCH_DB_KEY) -> Generator[Session, None, None]:
    get_sync_engine_instance(key)
    factory = _sync_session_factories[key]
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# --- External Utilities ---
@contextmanager
def sync_session_scope(key: str = DCH_DB_KEY) -> Generator[Session, None, None]:
    """Context manager for use in non-FastAPI scripts or background tasks."""
    yield from get_sync_session(key)


# --- FastAPI Dependencies (Path Aware) ---
def get_db_key_from_path(request: Request) -> str:
    """Parses URL path to determine DB key; falls back to DCH_DB_KEY."""
    path = request.url.path.lstrip("/")
    app_name = path.split("/", 1)[0] if path else ""
    db_key = (DCH_APPS.get(app_name) or {}).get("db_key") or DCH_DB_KEY
    return db_key if db_key in DATABASES else DCH_DB_KEY


def get_db(request: Request) -> Generator[Session, None, None]:
    """Main dependency for synchronous routes."""
    db_key = get_db_key_from_path(request)
    yield from get_sync_session(key=db_key)


async def get_async_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Main dependency for asynchronous routes."""
    db_key = get_db_key_from_path(request)
    async for session in get_async_session(key=db_key):
        yield session


def get_db_url(db_key: str = DCH_DB_KEY) -> str:
    db = DATABASES[db_key]

    user = db.get("user", "")
    password = quote_plus(db.get("password") or "")
    host = db.get("host") or "localhost"
    port = str(db.get("port") or "5432")
    name = db["name"]

    engine = db.get("engine", "postgresql")

    match engine:
        case "postgresql":
            db_engine = "postgresql+psycopg2"
        case _:
            db_engine = engine

    sqlalchemy_url = f"{db_engine}://{user}:{password}@{host}:{port}/{name}"
    # da_logger.debug(f"SQLAlchemy URL={sqlalchemy_url!r}")
    return sqlalchemy_url


# deps.py




class ExternalEngineRegistry:
    """
    Caches and manages external DB engines created from DBConnection records.
    Prevents recreating engines repeatedly.
    """

    _engines: Dict[str, Engine] = {}

    @classmethod
    def build_url(cls, conn: dict, is_pass_enc = True) -> str:
        """
        Build SQLAlchemy DB URL from connection params.
        Expected conn keys:
        engine, host, port, user, password, name
        """

        engine_type = conn["engine"]

        if engine_type == "postgresql":
            password = CryptoUtils().decrypt_txt(conn["password"]) if is_pass_enc else conn["password"]
            password = quote_plus(password)

            host = (
                "localhost"
                if os.environ.get("DATA_HOST") == conn["host"] and IS_LOCAL
                else conn["host"]
            )

            return (
                f"postgresql://{conn['user']}:{password}"
                f"@{host}:{conn['port']}/{conn['name']}"
            )

        elif engine_type == "sqlite":
            return f"sqlite:///{conn['file_path']}"

        else:
            raise ValueError(f"Unsupported DB engine: {engine_type}")

    @classmethod
    def get(cls, key: str, conn: dict) -> Engine:
        """
        Returns a cached Engine for a given connection key.
        """
        if key not in cls._engines:
            if conn == {} and key in DATABASES:
                if key not in DATABASES:
                    raise ValueError(f"Unsupported DB key: {key} , params of DB key {key} doesnt exist")
                conn = DATABASES[key]
                conn["password"] = CryptoUtils().encrypt_txt(conn["password"])

            db_url = cls.build_url(conn)

            cls._engines[key] = create_engine(
                db_url,
                pool_pre_ping=True,
                pool_recycle=1800,
                future=True,
            )

        return cls._engines[key]

    @classmethod
    def dispose(cls, key: str):
        """Dispose and remove a specific external engine."""
        if key in cls._engines:
            cls._engines[key].dispose()
            del cls._engines[key]

    @classmethod
    def dispose_all(cls):
        """Dispose all external engines."""
        for engine in cls._engines.values():
            engine.dispose()
        cls._engines.clear()




def terminate_idle_connections_for_engine(engine, idle_cutoff_minutes: int = 10) -> int:
    """Kill idle connections for one engine. Returns number killed."""
    if engine.dialect.name != "postgresql":
        return 0

    db_name = engine.url.database

    kill_stmt = text("""
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE state = 'idle'
          AND state_change < now() - (:minutes || ' minutes')::interval
          AND pid <> pg_backend_pid()
          AND datname = :db_name;
    """)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with SessionLocal() as session:
        res = session.execute(kill_stmt, {"minutes": str(idle_cutoff_minutes), "db_name": db_name})
        killed = res.scalars().all()
        session.commit()
        return len(killed)


def terminate_idle_connections(session: Session, idle_cutoff_minutes: int = 10) -> int:
    """Kill idle connections for the engine bound to this session."""
    engine = session.get_bind()
    return terminate_idle_connections_for_engine(engine, idle_cutoff_minutes)


def terminate_all_idle_connections(idle_cutoff_minutes: int = 10) -> Dict[str, int]:
    """
    Kill idle connections across:
      - internal sync engines in _sync_engines
      - external engines in ExternalEngineRegistry

    Returns dict: engine_key -> killed_count
    """
    results: Dict[str, int] = {}

    # 1) internal sync engines
    for key, engine in _sync_engines.items():
        try:
            results[f"internal:{key}"] = terminate_idle_connections_for_engine(engine, idle_cutoff_minutes)
        except Exception as e:
            da_logger.exception(f"Failed terminating idle connections for internal:{key}: {e}")
            results[f"internal:{key}"] = -1

    # 2) external engines
    for key, engine in ExternalEngineRegistry._engines.items():
        try:
            results[f"external:{key}"] = terminate_idle_connections_for_engine(engine, idle_cutoff_minutes)
        except Exception as e:
            da_logger.exception(f"Failed terminating idle connections for external:{key}: {e}")
            results[f"external:{key}"] = -1

    return results