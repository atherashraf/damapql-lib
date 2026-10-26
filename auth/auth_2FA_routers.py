
"""
DAMapQL Two-Factor Authorization Router.

Uses existing GoogleAuthenticatorManager and CLI management helpers.

Supports:
1. One-time OTP verification
2. Persistent form-scoped authorization sessions
3. Session validation
4. Session revocation
5. Permission-based authorization dependencies

All endpoints require DAMapQL JWT authentication.
"""

from __future__ import annotations

import hashlib
import os
import secrets

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
)
from pydantic import BaseModel, Field
from sqlalchemy import update

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.manage.manage_google_authenticator import _normalize_otp_identity, _get_authenticator, \
    _get_otp_manager
from app.damapql.auth.models import (
    OTPAuthenticator,
    OTPAuthorizationSession,
)
from app.damapql.auth.utils import AuthUtils




router_2fa = APIRouter(
    prefix="/2fa",
    tags=["2FA Authorization"],
)


# ============================================================
# Generic session configuration
# ============================================================

SESSION_LIFETIME_MINUTES = int(
    os.getenv("DAMAPQL_2FA_SESSION_LIFETIME_MINUTES", "240")
)

SESSION_IDLE_TIMEOUT_SECONDS = int(
    os.getenv("DAMAPQL_2FA_IDLE_TIMEOUT_SECONDS", "1800")
)


# ============================================================
# Request schemas
# ============================================================

class VerifyOTPRequest(BaseModel):
    application_code: str = Field(min_length=1, max_length=32)
    team_code: str = Field(min_length=1, max_length=32)
    otp: str = Field(pattern=r"^[0-9]{6}$")
    permission: str = Field(min_length=1, max_length=128)


class CreateSessionRequest(BaseModel):
    application_code: str = Field(min_length=1, max_length=32)
    team_code: str = Field(min_length=1, max_length=32)
    form_code: str = Field(min_length=1, max_length=100)
    otp: str = Field(pattern=r"^[0-9]{6}$")


# ============================================================
# Response schemas
# ============================================================

class VerifyOTPResponse(BaseModel):
    verified: bool
    permission: str


class Session2FAResponse(BaseModel):
    active: bool
    form_code: str
    permissions: list[str]
    expires_at: datetime


class CreateSessionResponse(Session2FAResponse):
    session_token: str


class CloseSessionResponse(BaseModel):
    closed: bool


# ============================================================
# Common helpers
# ============================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()


def _user_id(current_user: UserInfo) -> int:
    if (
        not current_user.is_active
        or not current_user.is_verified
    ):
        raise HTTPException(
            status_code=403,
            detail="User account inactive or unverified",
        )

    return int(current_user.id)


def _normalize_form_code(form_code: str) -> str:
    value = form_code.strip().upper()

    if not value:
        raise HTTPException(
            status_code=400,
            detail="Form code is required",
        )

    return value


def _normalize_permission(permission: str) -> str:
    value = permission.strip().lower()

    if not value:
        raise HTTPException(
            status_code=400,
            detail="Permission is required",
        )

    return value


def _get_active_team(
    session,
    application_code: str,
    team_code: str,
):
    application_code, team_code = _normalize_otp_identity(
        application_code,
        team_code,
    )

    try:
        team = _get_authenticator(
            session,
            application_code,
            team_code,
            lock=True,
        )
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Authenticator unavailable",
        )

    if not team.is_active:
        raise HTTPException(
            status_code=403,
            detail="Authenticator unavailable",
        )

    return team


def _consume_otp(
    session,
    team: OTPAuthenticator,
    otp: str,
) -> None:
    """
    Reuse the same verification logic as the CLI.

    The authenticator row must already be locked.
    The caller owns the transaction.
    """
    manager = _get_otp_manager()

    if manager.normalize_otp(otp) is None:
        raise HTTPException(
            status_code=400,
            detail="OTP must contain exactly six digits",
        )

    secret = manager.decrypt_secret(
        team.encrypted_totp_secret
    )

    step = manager.get_matched_step(
        secret=secret,
        otp_code=otp,
        valid_window=1,
        last_accepted_step=team.last_accepted_step,
    )

    if step is None:
        raise HTTPException(
            status_code=403,
            detail="Invalid, expired or previously used OTP",
        )

    team.last_accepted_step = step
    session.flush()


def _validate_session(
    session,
    *,
    user_id: int,
    form_code: str,
    token: str,
    required_permission: str | None = None,
    touch: bool = False,
) -> dict:
    """
    Validate a user-bound, form-scoped 2FA session.

    Effective permissions are the intersection of:
    - Permissions captured at session creation
    - Current authenticator permissions
    """
    form_code = _normalize_form_code(form_code)

    if not token or len(token) > 512:
        raise HTTPException(
            status_code=403,
            detail="Missing or invalid 2FA session",
        )

    record = (
        session.query(OTPAuthorizationSession)
        .filter(
            OTPAuthorizationSession.token_hash == _token_hash(token),
            OTPAuthorizationSession.user_id == user_id,
            OTPAuthorizationSession.form_code == form_code,
        )
        .with_for_update()
        .first()
    )

    now = _now()

    if (
        record is None
        or not record.is_valid(
            now=now,
            idle_timeout_seconds=SESSION_IDLE_TIMEOUT_SECONDS,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="2FA session expired or invalid",
        )

    team = session.get(
        OTPAuthenticator,
        record.authenticator_id,
    )

    if team is None or not team.is_active:
        raise HTTPException(
            status_code=403,
            detail="Team authenticator unavailable",
        )

    effective_permissions = (
        set(record.permissions or [])
        & set(team.permissions or [])
    )

    if required_permission is not None:
        required_permission = _normalize_permission(
            required_permission
        )

        if required_permission not in effective_permissions:
            raise HTTPException(
                status_code=403,
                detail="Required 2FA permission missing",
            )

    if touch:
        record.last_activity_at = now
        session.flush()

    return {
        "active": True,
        "form_code": form_code,
        "permissions": sorted(effective_permissions),
        "expires_at": _utc(record.expires_at),
    }


# ============================================================
# 1. One-time OTP verification
# ============================================================

@router_2fa.post(
    "/verify_otp",
    response_model=VerifyOTPResponse,
)
def verify_otp(
    payload: VerifyOTPRequest,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    """
    Verify a team OTP and a required permission.

    Consumes the OTP step.
    Does not create an authorization session.
    """
    _user_id(current_user)

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        team = _get_active_team(
            session,
            payload.application_code,
            payload.team_code,
        )

        permission = _normalize_permission(
            payload.permission
        )

        if permission not in set(team.permissions or []):
            raise HTTPException(
                status_code=403,
                detail="Team lacks required permission",
            )

        _consume_otp(
            session=session,
            team=team,
            otp=payload.otp,
        )

        session.commit()

        return VerifyOTPResponse(
            verified=True,
            permission=permission,
        )


# ============================================================
# 2. Create persistent authorization session
# ============================================================
def _get_team(
    session,
    application_code: str,
    team_code: str,
):
    application_code, team_code = _normalize_otp_identity(
        application_code,
        team_code,
    )

    try:
        return _get_authenticator(
            session,
            application_code,
            team_code,
            lock=True,
        )
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Authenticator unavailable",
        )

@router_2fa.post(
    "/create_session",
    response_model=CreateSessionResponse,
)
def create_session(
    payload: CreateSessionRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Verify OTP and create a persistent authorization session.

    Bound to:
    - Authenticated JWT user
    - Team authenticator
    - Form code
    - Permissions at creation time

    First successful OTP verification activates an authenticator
    that is pending enrollment.

    Suspended or administratively deactivated authenticators
    cannot reactivate themselves.
    """

    user_id = _user_id(current_user)
    form_code = _normalize_form_code(payload.form_code)

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        # --------------------------------------------------
        # 1. Retrieve authenticator with row-level lock.
        #    Do not require is_active=True yet.
        # --------------------------------------------------
        team = _get_team(
            session,
            payload.application_code,
            payload.team_code,
        )

        # --------------------------------------------------
        # 2. Validate authenticator lifecycle
        # --------------------------------------------------
        if team.suspended_at is not None:
            raise HTTPException(
                status_code=403,
                detail="Authenticator suspended",
            )

        if not team.is_active and team.verified_at is not None:
            raise HTTPException(
                status_code=403,
                detail="Authenticator administratively inactive",
            )

        # --------------------------------------------------
        # 3. Verify and consume OTP exactly once
        # --------------------------------------------------
        _consume_otp(
            session=session,
            team=team,
            otp=payload.otp,
        )

        now = _now()

        # --------------------------------------------------
        # 4. Automatically activate pending enrollment
        # --------------------------------------------------
        if not team.is_active:
            team.is_active = True
            team.verified_at = now
            team.updated_at = now

        # --------------------------------------------------
        # 5. Obtain current team permissions
        # --------------------------------------------------
        permissions = sorted(
            set(team.permissions or [])
        )

        # Do not issue a session with no permissions.
        if not permissions:
            raise HTTPException(
                status_code=403,
                detail="No permissions assigned to this authenticator",
            )

        # --------------------------------------------------
        # 6. Revoke previous sessions for this user/form
        # --------------------------------------------------
        session.execute(
            update(OTPAuthorizationSession)
            .where(
                OTPAuthorizationSession.user_id == user_id,
                OTPAuthorizationSession.form_code == form_code,
                OTPAuthorizationSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

        # --------------------------------------------------
        # 7. Generate secure authorization session token
        # --------------------------------------------------
        raw_token = secrets.token_urlsafe(48)

        record = OTPAuthorizationSession(
            authenticator_id=team.id,
            user_id=user_id,
            form_code=form_code,
            token_hash=_token_hash(raw_token),
            permissions=permissions,
            created_at=now,
            last_activity_at=now,
            expires_at=now + timedelta(
                minutes=SESSION_LIFETIME_MINUTES
            ),
        )

        session.add(record)

        # --------------------------------------------------
        # 8. Commit activation, OTP consumption, and session
        #    creation in one transaction
        # --------------------------------------------------
        session.flush()
        session.commit()

        return CreateSessionResponse(
            active=True,
            form_code=form_code,
            permissions=permissions,
            expires_at=_utc(record.expires_at),
            session_token=raw_token,
        )


# ============================================================
# 3. Validate existing authorization session
# ============================================================

@router_2fa.get(
    "/session",
    response_model=Session2FAResponse,
)
def get_session(
    form_code: str = Query(...),
    x_otp_session: Annotated[
        str | None,
        Header(alias="X-OTP-Session"),
    ] = None,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    """
    Validate an existing 2FA session.

    Does not consume another OTP.
    Does not refresh the inactivity timeout.
    """
    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        return _validate_session(
            session=session,
            user_id=_user_id(current_user),
            form_code=form_code,
            token=x_otp_session or "",
            touch=False,
        )


# ============================================================
# 4. Close authorization session
# ============================================================

@router_2fa.post(
    "/close",
    response_model=CloseSessionResponse,
)
def close_session(
    form_code: str = Query(...),
    x_otp_session: Annotated[
        str | None,
        Header(alias="X-OTP-Session"),
    ] = None,
    current_user: UserInfo = Depends(
        AuthUtils.get_current_user
    ),
):
    """
    Revoke a 2FA session belonging to the authenticated user.
    """
    user_id = _user_id(current_user)
    form_code = _normalize_form_code(form_code)

    token = x_otp_session or ""

    if not token or len(token) > 512:
        raise HTTPException(
            status_code=403,
            detail="Missing or invalid 2FA session",
        )

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        record = (
            session.query(OTPAuthorizationSession)
            .filter(
                OTPAuthorizationSession.token_hash == _token_hash(token),
                OTPAuthorizationSession.user_id == user_id,
                OTPAuthorizationSession.form_code == form_code,
            )
            .with_for_update()
            .first()
        )

        if record is None:
            raise HTTPException(
                status_code=403,
                detail="2FA session not found",
            )

        record.revoke()
        session.flush()
        session.commit()

        return CloseSessionResponse(closed=True)


# ============================================================
# 5. Reusable session permission dependency
# ============================================================

def require_2fa_permission(
    permission: str,
    form_code: str,
):
    """
    Protect an endpoint with an existing 2FA session.

    The permission and form code must be supplied by
    trusted backend code, not by client request parameters.
    """
    permission = _normalize_permission(permission)
    form_code = _normalize_form_code(form_code)

    def dependency(
        x_otp_session: Annotated[
            str | None,
            Header(alias="X-OTP-Session"),
        ] = None,
        current_user: UserInfo = Depends(
            AuthUtils.get_current_user
        ),
    ) -> dict:

        with OTPAuthenticator.managed_session(
            app_label="auth"
        ) as session:

            result = _validate_session(
                session=session,
                user_id=_user_id(current_user),
                form_code=form_code,
                token=x_otp_session or "",
                required_permission=permission,
                touch=True,
            )

            session.commit()

            return result

    return dependency
