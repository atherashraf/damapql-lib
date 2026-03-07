from types import SimpleNamespace

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
import app.dch.routers.styles as styles_module


def test_get_style_public_from_layer(client, monkeypatch):
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return SimpleNamespace(
                scalar_one_or_none=lambda: SimpleNamespace(layer_styling={"type": "simple", "color": "red"})
            )

    monkeypatch.setattr(styles_module.LayerInfo, "managed_session", lambda: FakeSession())

    response = client.get("/dch/get_style/layer-1/-1/")
    assert response.status_code == 200
    assert response.json()["payload"]["color"] == "red"


def test_save_style_allowed_for_app_manager(client, monkeypatch):
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

    class FakeMap:
        def __init__(self):
            self.layers = [{"uuid": "layer-1", "style": {"old": True}}]

        def save(self, session=None, allow_update=False):
            return self

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: FakeMap())

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user
    monkeypatch.setattr(styles_module.MapInfo, "managed_session", lambda: FakeSession())

    response = client.post(
        "/dch/save_style/layer-1/map-1/",
        json={"type": "simple", "color": "blue"},
    )

    assert response.status_code == 200
    assert "saved successfully" in response.json()["payload"].lower()
    client.app.dependency_overrides()


def test_save_style_forbidden_for_normal_user(client):
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

    response = client.post(
        "/dch/save_style/layer-1/map-1/",
        json={"type": "simple", "color": "blue"},
    )

    assert response.status_code == 403
    client.app.dependency_overrides.clear()