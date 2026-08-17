from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import redis

from app.damapql.cache.cache import (REDIS_NAMESPACE, RedisCache, RedisState, redis_cache, redis_client, )
from digitalarzengine.utils.singletons import da_logger

# Pre-register unlock script if Redis client is ready
_UNLOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""
_unlock_script = redis_client.register_script(_UNLOCK_LUA) if redis_client else None


class MVTCache:
    DEFAULT_TTL = RedisCache.DEFAULT_TTL
    LOCK_TIMEOUT = 15

    def __init__(self, cache: RedisCache | None = None):
        self.cache = cache or redis_cache

    @staticmethod
    def build_variant(cols: list[str] | None = None, resolution: float = 0, tile_size: int = 4096,
            feature_count: int | None = None, extra: dict[str, Any] | None = None, ) -> str:
        normalized: dict[str, Any] = {"cols": sorted(set(cols or [])), "resolution": resolution,
            "tile_size": tile_size, }

        if feature_count is not None:
            normalized["feature_count"] = feature_count

        if extra:
            normalized["extra"] = {str(k): str(v) for k, v in sorted(extra.items(), key=lambda item: str(item[0]))}

        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _tile_key(layer_uuid: str, x: int, y: int, z: int, variant: str) -> str:
        return f"{REDIS_NAMESPACE}:mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"

    @staticmethod
    def _lock_key(layer_uuid: str, x: int, y: int, z: int, variant: str) -> str:
        return f"{REDIS_NAMESPACE}:lock:mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"

    def get_cached_tile(self, layer_uuid: str, x: int, y: int, z: int, variant: str) -> bytes | None:
        key = self._tile_key(layer_uuid, x, y, z, variant)
        return self.cache.get(key)

    def set_cached_tile(self, layer_uuid: str, x: int, y: int, z: int, variant: str, mvt_bytes: bytes,
            ttl: int = DEFAULT_TTL, ) -> bool:
        if mvt_bytes is None:
            return False

        key = self._tile_key(layer_uuid, x, y, z, variant)
        return self.cache.set(key, mvt_bytes, ttl=ttl)

    def delete_cached_tile(self, layer_uuid: str, x: int, y: int, z: int, variant: str) -> bool:
        key = self._tile_key(layer_uuid, x, y, z, variant)
        return self.cache.delete(key)

    def acquire_tile_lock(self, layer_uuid: str, x: int, y: int, z: int, variant: str,
            timeout: int = LOCK_TIMEOUT, ) -> str | None:
        if redis_client is None or not RedisState.is_available():
            return "redis-unavailable"

        lock_key = self._lock_key(layer_uuid, x, y, z, variant)
        token = uuid.uuid4().hex

        try:
            acquired = redis_client.set(lock_key, token, nx=True, ex=timeout)
            RedisState.mark_available()
            return token if acquired else None

        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] LOCK key={lock_key}: {err}")
            return "redis-unavailable"

        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisLockError] key={lock_key}: {err}")
            return "redis-unavailable"

    def release_tile_lock(self, layer_uuid: str, x: int, y: int, z: int, variant: str, token: str | None, ) -> None:
        if (redis_client is None or not token or token == "redis-unavailable" or not RedisState.is_available()):
            return

        lock_key = self._lock_key(layer_uuid, x, y, z, variant)

        try:
            if _unlock_script is not None:
                _unlock_script(keys=[lock_key], args=[token])
            else:
                redis_client.eval(_UNLOCK_LUA, 1, lock_key, token)
            RedisState.mark_available()
        except (redis.ConnectionError, redis.TimeoutError) as err:
            RedisState.mark_unavailable()
            da_logger.warning(f"[RedisUnavailable] UNLOCK key={lock_key}: {err}")
        except Exception as err:
            RedisState.mark_unavailable()
            da_logger.exception(f"[RedisLockReleaseError] key={lock_key}: {err}")

    def clear_cache(self, layer_uuid: str | None = None) -> int:
        if layer_uuid:
            patterns = (f"{REDIS_NAMESPACE}:mvt:{layer_uuid}:*", f"{REDIS_NAMESPACE}:lock:mvt:{layer_uuid}:*",)
        else:
            patterns = (f"{REDIS_NAMESPACE}:mvt:*", f"{REDIS_NAMESPACE}:lock:mvt:*",)

        return sum(self.cache.clear(pattern) for pattern in patterns)


mvt_cache = MVTCache()
