from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
import app.auth.routers as auth_router_module


def test_swagger_login_success(client, monkeypatch):
    fake_user = UserInfo(
        id=1,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact="123",
    )

    monkeypatch.setattr(auth_router_module.User, "get_user_info", lambda username: fake_user)
    monkeypatch.setattr(auth_router_module.User, "get_password", lambda username: "encrypted")
    monkeypatch.setattr(auth_router_module.AuthUtils, "verify_password", lambda raw, enc: True)
    monkeypatch.setattr(auth_router_module.AuthUtils, "create_access_token", lambda username: "access-token")

    response = client.post(
        "/api/auth/swagger",   # <- adjust prefix
        data={"username": "ather", "password": "secret"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["access_token"] == "access-token"
    assert data["token_type"] == "bearer"


def test_swagger_login_invalid_username(client, monkeypatch):
    monkeypatch.setattr(auth_router_module.User, "get_user_info", lambda username: None)

    response = client.post(
        "/api/auth/swagger",   # <- adjust prefix
        data={"username": "wrong", "password": "secret"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == auth_router_module.INVALID_CREDENTIALS


def test_swagger_login_invalid_password(client, monkeypatch):
    fake_user = UserInfo(
        id=1,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact="123",
    )

    monkeypatch.setattr(auth_router_module.User, "get_user_info", lambda username: fake_user)
    monkeypatch.setattr(auth_router_module.User, "get_password", lambda username: "encrypted")
    monkeypatch.setattr(auth_router_module.AuthUtils, "verify_password", lambda raw, enc: False)

    response = client.post(
        "/api/auth/swagger",   # <- adjust prefix
        data={"username": "ather", "password": "wrong"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == auth_router_module.INVALID_CREDENTIALS


def test_login_json_success(client, monkeypatch):
    fake_user = UserInfo(
        id=1,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact="123",
    )

    monkeypatch.setattr(auth_router_module.User, "get_user_info", lambda username: fake_user)
    monkeypatch.setattr(auth_router_module.User, "get_password", lambda username: "encrypted")
    monkeypatch.setattr(auth_router_module.AuthUtils, "verify_password", lambda raw, enc: True)
    monkeypatch.setattr(auth_router_module.AuthUtils, "create_access_token", lambda username: "access-token")
    monkeypatch.setattr(auth_router_module.AuthUtils, "create_refresh_token", lambda username: "refresh-token")

    response = client.post(
        "/api/auth/jwt/login/json",   # <- adjust prefix
        json={"username": "ather", "password": "secret"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["token"]["accessToken"] == "access-token"
    assert data["token"]["refreshToken"] == "refresh-token"
    assert data["userInfo"]["username"] == "ather"
    assert data["userInfo"]["department"] == "survey"


def test_verify_token_success(client, monkeypatch):
    monkeypatch.setattr(auth_router_module.AuthUtils, "decode_token", lambda token: {"sub": "ather"})

    response = client.post("/api/auth/jwt/token/verify", json={"token": "valid-token"})
    assert response.status_code == 200
    assert response.json()["detail"] == "Token is valid"


def test_verify_token_invalid(client, monkeypatch):
    def raise_error(token):
        from jwt import InvalidTokenError
        raise InvalidTokenError("bad token")

    monkeypatch.setattr(auth_router_module.AuthUtils, "decode_token", raise_error)

    response = client.post("/api/auth/jwt/token/verify", json={"token": "invalid-token"})
    assert response.status_code == 401
    assert "invalid or expired token" in response.json()["detail"].lower()