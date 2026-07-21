from types import SimpleNamespace

from app.damapql.auth.utils import AuthUtils
from main import app
from app.damapql.auth.models import UserInfo

import app.damapql.auth.routers as auth_router_module


def _fake_user(**overrides):
    data = dict(
        id=1,
        username="manager",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        organization="Digital Arz",
        department="survey",
        is_active=True,
        is_verified=True,
        name="Manager",
        email="manager@example.com",
        contact=None,
    )
    data.update(overrides)
    return UserInfo(**data)


def test_create_user_forbidden_for_non_admin(client):
    def override_user():
        return UserInfo(
            id=10,
            username="normal_user",
            is_superuser=False,
            is_staff=True,
            roles=["data_editor"],
            organization="Digital Arz",
            department="survey",
            is_active=True,
            is_verified=True,
            name="Normal User",
            email="normal@example.com",
            contact=None,
        )

    app.dependency_overrides[AuthUtils.get_current_user] = override_user

    response = client.post(
        "/auth/create_user",
        json={
            "username": "newuser",
            "profile_name": "New User",
            "profile_email": "new@example.com",
            "profile_contact": "12345",
            "organization": "Digital Arz",
            "department": "survey",
            "roles": ["data_editor"],
        },
    )

    assert response.status_code == 403
    app.dependency_overrides.clear()


def test_create_user_success_for_app_manager(client, monkeypatch):
    def override_user():
        return _fake_user()

    class FakeManagedSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query(self, model):
            return self

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

        def refresh(self, obj):
            return None

        def rollback(self):
            return None

    fake_created_user = SimpleNamespace(
        to_object=lambda: UserInfo(
            id=99,
            username="newuser",
            is_superuser=False,
            is_staff=True,
            roles=["data_editor"],
            organization="Digital Arz",
            department="survey",
            is_active=False,
            is_verified=False,
            name="New User",
            email="new@example.com",
            contact="12345",
        )
    )

    app.dependency_overrides[AuthUtils.get_current_user] = override_user

    monkeypatch.setattr(
        auth_router_module.User,
        "managed_session",
        lambda session=None: FakeManagedSession(),
    )
    monkeypatch.setattr(auth_router_module.AuthUtils, "generate_temp_password", lambda n: "Temp123!")
    monkeypatch.setattr(auth_router_module.AuthUtils, "create_user", lambda **kwargs: fake_created_user)
    monkeypatch.setattr(
        auth_router_module.AuthUtils,
        "create_access_token",
        lambda subject, expires_delta=None: "activation-token",
    )
    monkeypatch.setattr(
        auth_router_module.EmailUtils,
        "get_email_subject_and_body",
        lambda email_type, params: ("Welcome", "Body"),
    )
    monkeypatch.setattr(
        auth_router_module.EmailUtils,
        "send_email_background",
        lambda *args, **kwargs: None,
    )

    response = client.post(
        "/auth/create_user",
        json={
            "username": "newuser",
            "profile_name": "New User",
            "profile_email": "new@example.com",
            "profile_contact": "12345",
            "organization": "Digital Arz",
            "department": "survey",
            "roles": ["data_editor"],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "newuser"
    assert data["organization"] == "Digital Arz"
    assert data["department"] == "survey"
    assert data["roles"] == ["data_editor"]

    app.dependency_overrides.clear()