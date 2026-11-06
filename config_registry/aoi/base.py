from __future__ import annotations
from typing import Any, ClassVar, Optional
from app.damapql.auth.dtypes import UserInfo


class BaseUserAOI:
    """
    Application-independent contract for User Area of Interest (AOI).
    Concrete implementations are registered during application startup.
    """
    _implementation: ClassVar[type["BaseUserAOI"] | None] = None

    @classmethod
    def register_implementation(cls, implementation: type["BaseUserAOI"]) -> None:
        if not issubclass(implementation, BaseUserAOI):
            raise TypeError(f"{implementation.__name__} must inherit from BaseUserAOI")
        cls._implementation = implementation

    @classmethod
    def get_implementation(cls) -> type["BaseUserAOI"]:
        return cls._implementation or NullUserAOI

    @classmethod
    def should_sync_from_payload(cls, payload: Any) -> bool:
        """Return True when the incoming user payload includes application AOI keys."""
        return False

    @classmethod
    def upsert_from_payload(
        cls,
        *,
        user_id: int,
        username: str,
        payload: Any,
    ) -> None:
        """Persist or update AOI assignments from user admin forms."""
        raise NotImplementedError

    @classmethod
    def attach_user_aoi(cls, user_obj: UserInfo) -> UserInfo:
        """Inject effective AOI properties onto the UserInfo token/session."""
        return user_obj

    @classmethod
    def get_user_aoi_geojson(cls, current_user: UserInfo) -> Any:
        """Return user AOI boundary geometry as GeoJSON (Feature or FeatureCollection)."""
        raise NotImplementedError

    @classmethod
    def get_user_aoi_wkt(cls, current_user: UserInfo) -> Optional[str]:
        """Return user AOI geometry directly as WKT in EPSG:3857."""
        raise NotImplementedError

    @classmethod
    def get_user_aoi_level(cls, user_obj: UserInfo) -> str:
        """Return identifier string representing the user's level tier."""
        return "province"

    @classmethod
    def delete_user_aoi(cls, user_id: int) -> None:
        """Clean up AOI records when an auth account is deleted."""
        return None


class NullUserAOI(BaseUserAOI):
    """Fallback when no AOI implementation is registered (unrestricted access)."""

    @classmethod
    def upsert_from_payload(cls, *, user_id: int, username: str, payload: Any) -> None:
        return None

    @classmethod
    def get_user_aoi_geojson(cls, current_user: UserInfo) -> None:
        return None

    @classmethod
    def get_user_aoi_wkt(cls, current_user: UserInfo) -> None:
        return None

    @classmethod
    def get_user_aoi_level(cls, user_obj: UserInfo) -> str:
        return "province"