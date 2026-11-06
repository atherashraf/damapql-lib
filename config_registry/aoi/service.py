# app/damapql/config_registry/aoi/service.py
from __future__ import annotations
from typing import Any, Optional

from app.damapql.auth.dtypes import UserInfo
from app.damapql.config_registry.aoi.base import BaseUserAOI


class UserAOIService:
    """
    Application-independent service for working with a user's Area of Interest.
    Delegates directly to the registered concrete implementation (e.g., WASA UserAOI).
    """

    @staticmethod
    def get_model() -> type[BaseUserAOI]:
        return BaseUserAOI.get_implementation()

    @classmethod
    def attach(cls, user_obj: UserInfo) -> UserInfo:
        return cls.get_model().attach_user_aoi(user_obj)

    @classmethod
    def sync(
        cls,
        *,
        user_id: int,
        username: str,
        payload: Any,
    ) -> None:
        model = cls.get_model()
        if not model.should_sync_from_payload(payload):
            return

        model.upsert_from_payload(
            user_id=user_id,
            username=username,
            payload=payload,
        )

    @classmethod
    def get_geojson(cls, user_obj: UserInfo) -> Any:
        return cls.get_model().get_user_aoi_geojson(user_obj)

    @classmethod
    def get_level(cls, user_obj: UserInfo) -> str:
        return cls.get_model().get_user_aoi_level(user_obj)

    @classmethod
    def get_wkt(cls, user_obj: UserInfo) -> Optional[str]:
        """
        Return the User's Area of Interest geometry as a WKT string.
        Delegates directly to the registered UserAOI implementation.
        """
        model = cls.get_model()
        if hasattr(model, "get_user_aoi_wkt"):
            return model.get_user_aoi_wkt(user_obj)
        return None

    @classmethod
    def delete(cls, user_id: int) -> None:
        cls.get_model().delete_user_aoi(user_id)