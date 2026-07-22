from fastapi import HTTPException

from app.damapql.auth.models import UserInfo
from app.damapql.auth.utils import AuthUtils


def _fake_user(**overrides):
    data = dict(
        id=1,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        organization="Digital Arz",
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact=None,
    )
    data.update(overrides)
    return UserInfo(**data)


def test_has_role_superuser():
    user = _fake_user(
        username="admin",
        is_superuser=True,
        roles=[],
        organization=None,
        department=None,
        name="Admin",
        email="admin@example.com",
    )

    assert AuthUtils.has_role(user, "anything") is True


def test_has_role_normalized_match():
    user = _fake_user(
        id=2,
        roles=["app_manager", "data_editor"],
    )

    assert AuthUtils.has_role(user, "APP_MANAGER") is True
    assert AuthUtils.has_role(user, " data_editor ") is True
    assert AuthUtils.has_role(user, "reviewer") is False


def test_require_role_allows_matching_user():
    user = _fake_user(
        id=2,
        roles=["app_manager"],
    )

    checker = AuthUtils.require_role("app_manager")
    result = checker(user=user)
    assert result.username == "ather"


def test_require_role_blocks_non_matching_user():
    user = _fake_user(
        id=2,
        roles=["data_editor"],
    )

    checker = AuthUtils.require_role("app_manager")

    try:
        checker(user=user)
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "required" in exc.detail.lower()


def test_require_department_allows_matching_user():
    user = _fake_user(
        id=3,
        username="user1",
        roles=["data_editor"],
        department="survey",
        name="User One",
        email="user1@example.com",
    )

    checker = AuthUtils.require_department("Survey")
    result = checker(user=user)
    assert result.username == "user1"


def test_require_department_blocks_non_matching_user():
    user = _fake_user(
        id=3,
        username="user1",
        roles=["data_editor"],
        department="planning",
        name="User One",
        email="user1@example.com",
    )

    checker = AuthUtils.require_department("survey")

    try:
        checker(user=user)
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "department" in exc.detail.lower()