from fastapi import HTTPException

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils


def test_has_role_superuser():
    user = UserInfo(
        id=1,
        username="admin",
        is_superuser=True,
        is_staff=True,
        roles=[],
        department=None,
        is_active=True,
        is_verified=True,
        name="Admin",
        email="admin@example.com",
        contact=None,
    )

    assert AuthUtils.has_role(user, "anything") is True


def test_has_role_normalized_match():
    user = UserInfo(
        id=2,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager", "data_editor"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact=None,
    )

    assert AuthUtils.has_role(user, "APP_MANAGER") is True
    assert AuthUtils.has_role(user, " data_editor ") is True
    assert AuthUtils.has_role(user, "reviewer") is False


def test_require_role_allows_matching_user():
    user = UserInfo(
        id=2,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["app_manager"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact=None,
    )

    checker = AuthUtils.require_role("app_manager")
    result = checker(user=user)
    assert result.username == "ather"


def test_require_role_blocks_non_matching_user():
    user = UserInfo(
        id=2,
        username="ather",
        is_superuser=False,
        is_staff=True,
        roles=["data_editor"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="Ather",
        email="ather@example.com",
        contact=None,
    )

    checker = AuthUtils.require_role("app_manager")

    try:
        checker(user=user)
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "required" in exc.detail.lower()


def test_require_department_allows_matching_user():
    user = UserInfo(
        id=3,
        username="user1",
        is_superuser=False,
        is_staff=True,
        roles=["data_editor"],
        department="survey",
        is_active=True,
        is_verified=True,
        name="User One",
        email="user1@example.com",
        contact=None,
    )

    checker = AuthUtils.require_department("Survey")
    result = checker(user=user)
    assert result.username == "user1"


def test_require_department_blocks_non_matching_user():
    user = UserInfo(
        id=3,
        username="user1",
        is_superuser=False,
        is_staff=True,
        roles=["data_editor"],
        department="planning",
        is_active=True,
        is_verified=True,
        name="User One",
        email="user1@example.com",
        contact=None,
    )

    checker = AuthUtils.require_department("survey")

    try:
        checker(user=user)
        assert False, "Expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "department" in exc.detail.lower()