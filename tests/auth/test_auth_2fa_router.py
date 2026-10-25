"""HTTP tests for DAMapQL 2FA routes; follows test_auth_router.py conventions.

Uses the project's existing ``client`` fixture and overrides JWT authentication.
No PostgreSQL connection, encryption key, or real TOTP is required.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI

from app.damapql.auth.models import UserInfo
from app.damapql.auth.utils import AuthUtils
import app.damapql.auth.auth_2FA_routers as twofa_module
from main import app


BASE_URL = "/auth/2fa"
FORM = "RAM_OPTIMIZATION"
PERMISSION = "lbdc:advisory:execute"
TOKEN = "test-session-token"


def _fake_user(**overrides):
    data = dict(
        id=1, username="ather", is_superuser=False, is_staff=True,
        roles=["app_manager"], organization="Digital Arz",
        department="survey", is_active=True, is_verified=True,
        name="Ather", email="ather@example.com", contact=None,
    )
    data.update(overrides)
    return UserInfo(**data)


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.added = []
        self.record = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def flush(self):
        pass

    def add(self, value):
        self.added.append(value)

    def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    def query(self, model):
        return self

    def filter(self, *args):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self.record

    def get(self, model, pk):
        return self.team


@pytest.fixture
def twofa_env(monkeypatch):
    """Mock database and OTP internals; keep real HTTP route and schemas."""
    db = FakeSession()
    team = SimpleNamespace(
        id=10, application_code="LBDC", code="RAM_OPERATORS",
        is_active=True, permissions=["lbdc:advisory:tune", PERMISSION],
        encrypted_totp_secret="encrypted", last_accepted_step=None,
    )
    db.team = team
    app.dependency_overrides[AuthUtils.get_current_user] = lambda: _fake_user()
    monkeypatch.setattr(
        twofa_module.OTPAuthenticator, "managed_session",
        lambda **kwargs: db,
    )
    monkeypatch.setattr(
        twofa_module, "_get_active_team",
        lambda session, application_code, team_code: team,
    )
    monkeypatch.setattr(twofa_module, "_consume_otp", lambda **kwargs: None)
    yield SimpleNamespace(db=db, team=team)
    app.dependency_overrides.pop(AuthUtils.get_current_user, None)


def _create_payload(**overrides):
    data = dict(
        application_code="LBDC", team_code="RAM_OPERATORS",
        form_code=FORM, otp="123456",
    )
    data.update(overrides)
    return data


def _verify_payload(**overrides):
    data = dict(
        application_code="LBDC", team_code="RAM_OPERATORS",
        permission=PERMISSION, otp="123456",
    )
    data.update(overrides)
    return data


def _existing_record(**overrides):
    now = datetime.now(timezone.utc)
    data = dict(
        authenticator_id=10,
        permissions=[PERMISSION],
        expires_at=now + timedelta(hours=1),
        last_activity_at=now - timedelta(minutes=2),
        revoked_at=None,
    )
    data.update(overrides)
    record = SimpleNamespace(**data)
    record.is_valid = lambda **kwargs: (
        record.revoked_at is None
        and record.expires_at > kwargs["now"]
    )
    record.revoke = lambda: setattr(record, "revoked_at", datetime.now(timezone.utc))
    return record


def test_verify_otp_success(client, twofa_env):
    response = client.post(f"{BASE_URL}/verify_otp", json=_verify_payload())
    assert response.status_code == 200, response.text
    assert response.json() == {"verified": True, "permission": PERMISSION}
    assert twofa_env.db.commits == 1


def test_verify_otp_permission_denied(client, twofa_env):
    twofa_env.team.permissions = ["lbdc:advisory:tune"]
    response = client.post(f"{BASE_URL}/verify_otp", json=_verify_payload())
    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()
    assert twofa_env.db.commits == 0


def test_verify_otp_invalid_code_schema(client, twofa_env):
    response = client.post(
        f"{BASE_URL}/verify_otp", json=_verify_payload(otp="123")
    )
    assert response.status_code == 422


def test_verify_otp_rejected_by_manager(client, twofa_env, monkeypatch):
    from fastapi import HTTPException

    def reject(**kwargs):
        raise HTTPException(403, "Invalid, expired or previously used OTP")

    monkeypatch.setattr(twofa_module, "_consume_otp", reject)
    response = client.post(f"{BASE_URL}/verify_otp", json=_verify_payload())
    assert response.status_code == 403
    assert "otp" in response.json()["detail"].lower()
    assert twofa_env.db.commits == 0


def test_create_session_success(client, twofa_env):
    response = client.post(f"{BASE_URL}/create_session", json=_create_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active"] is True
    assert body["form_code"] == FORM
    assert PERMISSION in body["permissions"]
    assert body["session_token"]
    assert body["expires_at"]
    assert len(twofa_env.db.added) == 1
    assert twofa_env.db.commits == 1
    # Only the hash is persisted; never persist the bearer token itself.
    assert twofa_env.db.added[0].token_hash == twofa_module._token_hash(body["session_token"])


def test_create_session_invalid_payload(client, twofa_env):
    response = client.post(
        f"{BASE_URL}/create_session", json=_create_payload(otp="not-an-otp")
    )
    assert response.status_code == 422
    assert twofa_env.db.commits == 0


def test_create_session_inactive_team(client, twofa_env, monkeypatch):
    from fastapi import HTTPException

    def reject(*args):
        raise HTTPException(403, "Authenticator unavailable")

    monkeypatch.setattr(twofa_module, "_get_active_team", reject)
    response = client.post(f"{BASE_URL}/create_session", json=_create_payload())
    assert response.status_code == 403
    assert response.json()["detail"] == "Authenticator unavailable"


def test_session_valid(client, twofa_env):
    twofa_env.db.record = _existing_record()
    response = client.get(
        f"{BASE_URL}/session",
        params={"form_code": FORM},
        headers={"X-OTP-Session": TOKEN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active"] is True
    assert response.json()["permissions"] == [PERMISSION]
    assert twofa_env.db.commits == 0


def test_session_missing_token(client, twofa_env):
    response = client.get(f"{BASE_URL}/session", params={"form_code": FORM})
    assert response.status_code == 403


def test_session_expired(client, twofa_env):
    twofa_env.db.record = _existing_record(
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    response = client.get(
        f"{BASE_URL}/session", params={"form_code": FORM},
        headers={"X-OTP-Session": TOKEN},
    )
    assert response.status_code == 403


def test_close_session_success(client, twofa_env):
    record = _existing_record()
    twofa_env.db.record = record
    response = client.post(
        f"{BASE_URL}/close", params={"form_code": FORM},
        headers={"X-OTP-Session": TOKEN},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"closed": True}
    assert record.revoked_at is not None
    assert twofa_env.db.commits == 1


def test_close_session_unknown_token(client, twofa_env):
    response = client.post(
        f"{BASE_URL}/close", params={"form_code": FORM},
        headers={"X-OTP-Session": TOKEN},
    )
    assert response.status_code == 403


def test_permission_dependency_allows(client, twofa_env):
    """Exercise the dependency through FastAPI, not by calling it directly."""
    twofa_env.db.record = _existing_record()
    protected = FastAPI()

    @protected.get("/protected")
    def protected_route(auth=Depends(twofa_module.require_2fa_permission(PERMISSION, FORM))):
        return {"allowed": auth["active"]}

    protected.dependency_overrides[AuthUtils.get_current_user] = lambda: _fake_user()
    from fastapi.testclient import TestClient
    with TestClient(protected) as test_client:
        response = test_client.get("/protected", headers={"X-OTP-Session": TOKEN})
    assert response.status_code == 200, response.text
    assert response.json() == {"allowed": True}
    assert twofa_env.db.commits == 1


def test_permission_dependency_denies_missing_permission(client, twofa_env):
    twofa_env.db.record = _existing_record()
    twofa_env.team.permissions = ["lbdc:advisory:tune"]
    protected = FastAPI()

    @protected.get("/protected")
    def protected_route(auth=Depends(twofa_module.require_2fa_permission(PERMISSION, FORM))):
        return {"allowed": True}

    protected.dependency_overrides[AuthUtils.get_current_user] = lambda: _fake_user()
    from fastapi.testclient import TestClient
    with TestClient(protected) as test_client:
        response = test_client.get("/protected", headers={"X-OTP-Session": TOKEN})
    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()
