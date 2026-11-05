"""
Application-independent service for working with a user's Area of Interest.

DAMapQL calls this service instead of importing an application-specific
UserAOI model directly.

The concrete implementation is registered during application bootstrap:

    # app/wasa/app_extension.py

    from fastapi import FastAPI

    from app.damapql.auth.user_aoi import BaseUserAOI
    from app.wasa.models.admin import UserAOI


    def configure_wasa_extension(app: FastAPI) -> None:
        BaseUserAOI.register_implementation(UserAOI)

This allows the same DAMapQL authentication module to work with different
AOI implementations, for example:

    WASA:
        District -> WASA Division -> WASA Subdivision

    Irrigation:
        Zone -> Circle -> Division

    Administration:
        District -> Tehsil -> Union Council

    Revenue:
        District -> Tehsil -> Mauza

If an application does not register an implementation,
BaseUserAOI returns NullUserAOI. This allows DAMapQL authentication to
continue working without AOI functionality.
"""
from typing import Any

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.user_aoi import BaseUserAOI


class UserAOIService:

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
    def get_geojson(cls, user_obj: UserInfo):
        return cls.get_model().get_user_aoi_geojson(user_obj)

    @classmethod
    def get_level(cls, user_obj: UserInfo) -> str:
        return cls.get_model().get_user_aoi_level(user_obj)

    @classmethod
    def delete(cls, user_id: int) -> None:
        cls.get_model().delete_user_aoi(user_id)

    @classmethod
    def get_wkt(cls, user_obj: UserInfo) -> str | None:
        """
        Return the User's Area of Interest geometry as a WKT string.
        Delegates to the registered concrete UserAOI implementation.
        """
        model = cls.get_model()
        if hasattr(model, "get_user_aoi_wkt"):
            return model.get_user_aoi_wkt(user_obj)
        return None