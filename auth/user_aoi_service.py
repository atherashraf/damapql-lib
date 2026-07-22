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