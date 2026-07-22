from app.damapql.auth.models import UserInfo
from app.damapql.auth.utils import AuthUtils
import app.damapql.dch.routers.tiles as tiles_module


def test_clear_cache_forbidden_for_normal_user(client):
    def override_user():
        return UserInfo(
            id=2,
            username="user1",
            is_superuser=False,
            is_staff=True,
            roles=["data_editor"],
            department="gis",
            is_active=True,
            is_verified=True,
            name="User",
            email="user@example.com",
            contact=None,
        )

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    response = client.delete("/dch/clear_cache/123e4567-e89b-12d3-a456-426614174000/")
    assert response.status_code == 403

    client.app.dependency_overrides.clear()


def test_clear_cache_allowed_for_app_manager(client, monkeypatch):
    def override_user():
        return UserInfo(
            id=1,
            username="manager",
            is_superuser=False,
            is_staff=True,
            roles=["app_manager"],
            department="gis",
            is_active=True,
            is_verified=True,
            name="Manager",
            email="manager@example.com",
            contact=None,
        )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user
    monkeypatch.setattr(tiles_module.DBBase, "get_engine", lambda app_label=None: object())
    monkeypatch.setattr(tiles_module, "Session", lambda engine: FakeSession())

    response = client.delete("/dch/clear_cache/123e4567-e89b-12d3-a456-426614174000/")
    assert response.status_code == 200
    assert "successfully cleared" in response.json()["payload"].lower()

    client.app.dependency_overrides.clear()