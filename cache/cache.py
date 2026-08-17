from __future__ import annotations

import os
import time
from datetime import date

import redis

from digitalarzengine.utils.singletons import da_logger

# -------------------------------------------------------------------
# REDIS CONFIGURATION
# -------------------------------------------------------------------

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_NAMESPACE = os.getenv("APP_NAME", "damapql").strip().lower()

# -------------------------------------------------------------------
# REDIS CLIENT
# -------------------------------------------------------------------

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,  # Fixed: Use configured REDIS_DB
        decode_responses=False, socket_connect_timeout=0.05, socket_timeout=1.0, health_check_interval=30, )
except Exception as err:
    redis_client = None
    da_logger.warning(f"[RedisInitializationError] {err}")


# -------------------------------------------------------------------
# CIRCUIT BREAKER
# -------------------------------------------------------------------


class RedisState:
    FAILURE_COOLDOWN = 30.0
    _unavailable_until: float = 0.0

    @classmethod
    def is_available(cls) -> bool:
        return time.monotonic() >= cls._unavailable_until

    @classmethod
    def mark_unavailable(cls) -> None:
        cls._unavailable_until = time.monotonic() + cls.FAILURE_COOLDOWN

    @classmethod
    def mark_available(cls) -> None:
        cls._unavailable_until = 0.0


# -------------------------------------------------------------------
# REVISION TRACKER
# -------------------------------------------------------------------


class CacheRevision:
    @staticmethod
    def _key(resource: str, reference: str | date) -> str:
        if isinstance(reference, date):
            reference = reference.isoformat()
        return f"{REDIS_NAMESPACE}:revision:{resource}:{reference}"

    @classmethod
    def get(cls, resource: str, reference: str | date) -> int | None:
        if redis_client is None or not RedisState.is_available():
            return None

        key = cls._key(resource, reference)
        try:
            value = redis_client.get(key)
            RedisState.mark_available()
            return int(value) if value is not None else 0

        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[CacheRevisionUnavailable] resource={resource} | reference={reference} | {err}")
            return None
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[CacheRevisionReadError] resource={resource} | reference={reference} | {err}")
            return None

    @classmethod
    def bump(cls, resource: str, reference: str | date) -> int | None:
        if redis_client is None or not RedisState.is_available():
            return None

        key = cls._key(resource, reference)
        try:
            revision = redis_client.incr(key)
            RedisState.mark_available()
            da_logger.debug(f"[CacheRevisionBump] resource={resource} | reference={reference} | revision={revision}")
            return int(revision)

        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(
                f"[CacheRevisionUnavailable] Could not bump revision | resource={resource} | reference={reference} | {err}")
            return None
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[CacheRevisionBumpError] resource={resource} | reference={reference} | {err}")
            return None


# -------------------------------------------------------------------
# APPLICATION CACHE
# -------------------------------------------------------------------


class RedisCache:
    DEFAULT_TTL = 60 * 60 * 24 * 7

    @staticmethod
    def is_available() -> bool:
        if redis_client is None or not RedisState.is_available():
            return False
        try:
            is_alive = bool(redis_client.ping())
            if is_alive:
                RedisState.mark_available()
            return is_alive
        except (redis.ConnectionError, redis.TimeoutError):
            RedisState.mark_unavailable()
            return False
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisAvailabilityError] {err}")
            return False

    def get(self, key: str) -> bytes | None:
        if redis_client is None or not RedisState.is_available():
            return None

        try:
            value = redis_client.get(key)
            RedisState.mark_available()
            return value
        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] GET key={key}: {err}")
            return None
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisReadError] GET key={key}: {err}")
            return None

    def set(self, key: str, value: bytes, ttl: int = DEFAULT_TTL) -> bool:
        if redis_client is None or value is None or not RedisState.is_available():
            return False

        try:
            result = redis_client.set(key, value, ex=ttl)
            RedisState.mark_available()
            return bool(result)
        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] SET key={key}: {err}")
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisCacheWriteError] key={key}: {err}")
        return False

    def delete(self, key: str) -> bool:
        if redis_client is None or not RedisState.is_available():
            return False

        try:
            result = bool(redis_client.delete(key))
            RedisState.mark_available()
            return result
        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] DELETE key={key}: {err}")
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisCacheDeleteError] key={key}: {err}")
        return False

    def clear(self, pattern: str | None = None) -> int:
        if redis_client is None or not RedisState.is_available():
            return 0

        pattern = pattern or f"{REDIS_NAMESPACE}:*"
        deleted = 0
        batch: list[bytes] = []

        try:
            for key in redis_client.scan_iter(match=pattern, count=1000):
                batch.append(key)
                if len(batch) >= 1000:
                    deleted += int(redis_client.delete(*batch))
                    batch.clear()

            if batch:
                deleted += int(redis_client.delete(*batch))

            RedisState.mark_available()
        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] CLEAR pattern={pattern}: {err}")
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisCacheClearError] pattern={pattern}: {err}")

        return deleted


redis_cache = RedisCache()
