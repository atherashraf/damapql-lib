from __future__ import annotations

from typing import Any, ClassVar

from app.damapql.auth.dtypes import UserInfo


class BaseUserAOI:
    """
    Application-independent contract for user AOI implementations.

    This is intentionally not a SQLAlchemy declarative base. Concrete
    implementations can inherit from WASABase, LBDCBase, GeoCADBase, etc.
    """

    _implementation: ClassVar[type["BaseUserAOI"] | None] = None

    @classmethod
    def register_implementation(
        cls,
        implementation: type["BaseUserAOI"],
    ) -> None:
        if not issubclass(implementation, BaseUserAOI):
            raise TypeError(
                f"{implementation.__name__} must inherit from BaseUserAOI"
            )

        cls._implementation = implementation

    @classmethod
    def get_implementation(cls) -> type["BaseUserAOI"]:
        return cls._implementation or NullUserAOI

    @classmethod
    def should_sync_from_payload(cls, payload: Any) -> bool:
        """
        Return True when the request contains application-specific AOI data.

        Concrete implementations decide which fields constitute AOI data.
        """
        return False

    @classmethod
    def upsert_from_payload(
        cls,
        *,
        user_id: int,
        username: str,
        payload: Any,
    ) -> None:
        """
        Create, update or delete AOI information using the application's
        request payload.
        """
        raise NotImplementedError

    @classmethod
    def attach_user_aoi(cls, user_obj: UserInfo) -> UserInfo:
        """
        Populate UserInfo with the user's effective AOI.
        """
        return user_obj

    @classmethod
    def get_user_aoi_geojson(cls, current_user: UserInfo):
        """
        Return the user's effective AOI geometry.
        """
        raise NotImplementedError

    @classmethod
    def get_user_aoi_wkt(cls, current_user: UserInfo) -> str | None:
        """
        Return the user's effective AOI geometry as a WKT string.
        """
        raise NotImplementedError

    @classmethod
    def get_user_aoi_level(cls, user_obj: UserInfo) -> str:
        """
        Return the user's effective AOI level.
        """
        return "unrestricted"

    @classmethod
    def delete_user_aoi(cls, user_id: int) -> None:
        """
        Remove AOI data when an auth user is deleted.
        """
        return None


class NullUserAOI(BaseUserAOI):
    """
    Used when an application does not configure AOI functionality.
    """

    @classmethod
    def upsert_from_payload(
        cls,
        *,
        user_id: int,
        username: str,
        payload: Any,
    ) -> None:
        return None

    @classmethod
    def get_user_aoi_geojson(cls, current_user: UserInfo):
        return None