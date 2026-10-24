# app.damapql.auth.manage.manage_google_authenticator
"""
DAMapQL Google Authenticator CLI Management

Examples:

Generate encryption key:
    python -m app.damapql.manage generate_crypto_key
    set DAMAPQL_OTP_ENCRYPTION_KEY in .env  to proceed further

Create authenticator:
    python -m app.damapql.manage create_otp_authenticator \
    --application_code LBDC --code PID --name "PID PMIU Team" \
    --permissions "lbdc:advisory:tune,lbdc:advisory:execute"


Verify:
     python -m app.damapql.manage verify_otp_authenticator \
     --application_code LBDC --code PID

Show authenticator:
    python -m app.damapql.manage show_otp_authenticator -\
    -application_code LBDC --code PID --show_setup_key false


Recover QR from stored secret:
    python -m app.damapql.manage regenerate_otp_qr \
    --application_code LBDC --code PID


change QR and stored key using rotate_otp_authenticator:
    python -m app.damapql.manage rotate_otp_authenticator \
    --application_code LBDC --code PID

Deactivate authenticator:
    python -m app.damapql.manage deactivate_otp_authenticator \
    --application_code LBDC --code PID

"""

from __future__ import annotations

import base64
import getpass
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import update

from app.damapql.auth.models import OTPAuthenticator, OTPAuthorizationSession
from app.damapql.auth.manager.google_authenticator_manager import GoogleAuthenticatorManager


def _get_otp_manager() -> GoogleAuthenticatorManager:
    key = os.environ.get("DAMAPQL_OTP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("DAMAPQL_OTP_ENCRYPTION_KEY is not configured")
    return GoogleAuthenticatorManager(encryption_key=key)


def _normalize_otp_identity(application_code: str, code: str) -> tuple[str, str]:
    return GoogleAuthenticatorManager.normalize_identity(application_code, code)



def _parse_otp_permissions(
    permissions: str | None,
    application_code: str,
) -> list[str]:
    """
    Parse optional comma-separated permissions.

    Examples:
        None -> []
        "" -> []
        "lbdc:advisory:execute" -> ["lbdc:advisory:execute"]

    An empty permission list means the authenticator
    has no authorization privileges yet.
    """

    if not permissions or not permissions.strip():
        return []

    permission_list = list(
        dict.fromkeys(
            permission.strip().lower()
            for permission in permissions.split(",")
            if permission.strip()
        )
    )

    expected_prefix = application_code.lower() + ":"

    for permission in permission_list:
        if not permission.startswith(expected_prefix):
            raise ValueError(
                f"Permission '{permission}' must start "
                f"with '{expected_prefix}'"
            )

        if not re.fullmatch(
            r"[a-z0-9_-]+(?::[a-z0-9_-]+)+",
            permission,
        ):
            raise ValueError(
                f"Invalid permission format: {permission}"
            )

    return permission_list


def _get_otp_qr_path(application_code: str, code: str, output: str) -> Path:
    directory = Path(output).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    return directory / f"{application_code}_{code}_otp.png"


def _save_otp_qr(qr_code_base64: str, qr_path: Path) -> Path:
    """Exclusive creation. On Windows, secure the directory ACL separately."""
    qr_bytes = base64.b64decode(qr_code_base64, validate=True)
    if not qr_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Enrollment image is not a PNG")
    fd = os.open(str(qr_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(qr_bytes)
    except Exception:
        qr_path.unlink(missing_ok=True)
        raise
    return qr_path


def _get_authenticator(session, application_code: str, code: str, *, lock: bool = False):
    query = session.query(OTPAuthenticator).filter_by(
        application_code=application_code, code=code
    )
    if lock:
        query = query.with_for_update()
    authenticator = query.first()
    if authenticator is None:
        raise ValueError(f"Authenticator {application_code}:{code} not found")
    return authenticator


def _revoke_sessions(session, authenticator_id: int) -> int:
    """Revoke all outstanding form sessions, including timestamp."""
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(OTPAuthorizationSession)
        .where(
            OTPAuthorizationSession.authenticator_id == authenticator_id,
            OTPAuthorizationSession.revoked.is_(False),
        )
        .values(revoked=True, revoked_at=now)
    )
    return result.rowcount or 0


def create_otp_authenticator(
    application_code: str,
    code: str,
    name: str,
    permissions: str = "",
    output: str = "otp_enrollment",
):
    """Enroll a new team; remains inactive until a valid OTP is verified."""
    application_code, code = _normalize_otp_identity(application_code, code)
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        raise ValueError("Authenticator name is required")
    permission_list = _parse_otp_permissions(permissions, application_code)
    manager = _get_otp_manager()
    qr_path = _get_otp_qr_path(application_code, code, output)
    if qr_path.exists():
        raise FileExistsError(f"QR file already exists: {qr_path}")

    enrollment = manager.create_enrollment(application_code, code)
    image = manager.generate_enrollment_qr(
        provisioning_uri=enrollment.provisioning_uri,
        secret=enrollment.secret,
        application_code=application_code,
        code=code,
        name=name,
    )
    # Save image before DB commit; remove it if DB transaction fails.
    _save_otp_qr(image, qr_path)
    try:
        with OTPAuthenticator.managed_session(app_label="auth") as session:
            existing = session.query(OTPAuthenticator.id).filter_by(
                application_code=application_code, code=code
            ).first()
            if existing:
                raise ValueError(f"Authenticator {application_code}:{code} already exists")
            record = OTPAuthenticator(
                application_code=application_code,
                code=code,
                name=name,
                encrypted_totp_secret=manager.encrypt_secret(enrollment.secret),
                permissions=permission_list,
                is_active=False,
                last_accepted_step=None,
            )
            session.add(record)
            session.flush()
            record_id = record.id
            session.commit()
    except Exception:
        qr_path.unlink(missing_ok=True)
        raise

    print(f"\nCreated authenticator #{record_id}: {application_code}:{code}")
    print(f"QR + manual setup key: {qr_path}")
    print("Status: INACTIVE (verify enrollment to activate)")
    print("Setup key is intentionally not printed in terminal output.")
    print("Protect the PNG, enroll the phones, then securely remove the PNG.")


def show_otp_authenticator(
    application_code: str,
    code: str,
    show_setup_key: str = "false",
):
    """Show metadata, optionally reveal the existing secret after confirmation."""
    application_code, code = _normalize_otp_identity(application_code, code)
    flag = str(show_setup_key).strip().lower()
    if flag not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("show_setup_key must be true or false")
    reveal = flag in {"true", "1", "yes"}
    with OTPAuthenticator.managed_session(app_label="auth") as session:
        record = _get_authenticator(session, application_code, code)
        print("\nDAMapQL Team TOTP Authenticator")
        print(f"ID          : {record.id}")
        print(f"Application : {record.application_code}")
        print(f"Team        : {record.code}")
        print(f"Name        : {record.name}")
        print(f"Active      : {record.is_active}")
        print(f"Permissions : {record.permissions}")
        print(f"Created     : {record.created_at}")
        print(f"Updated     : {record.updated_at}")
        if reveal:
            if input("Reveal team TOTP setup key? Type REVEAL: ").strip() != "REVEAL":
                print("Cancelled.")
                return
            manager = _get_otp_manager()
            secret = manager.decrypt_secret(record.encrypted_totp_secret)
            print(f"Account     : {application_code}:{code}")
            print(f"Setup Key   : {manager._format_setup_key(secret)}")
            print("TOTP        : SHA1 / 6 digits / 30 seconds")
            print("CONFIDENTIAL: Do not share outside the authorized team.")


def verify_otp_authenticator(application_code: str, code: str):
    """Activate enrollment or test a new unused OTP for an active team.

    This is an admin CLI command, NOT a form-unlock operation.
    A successfully tested OTP consumes its time step for the whole team.
    """
    application_code, code = _normalize_otp_identity(application_code, code)
    manager = _get_otp_manager()
    otp = getpass.getpass("Enter Google Authenticator 6-digit code: ").strip()
    if manager.normalize_otp(otp) is None:
        raise ValueError("OTP must contain exactly six digits")
    with OTPAuthenticator.managed_session(app_label="auth") as session:
        record = _get_authenticator(session, application_code, code, lock=True)
        already_active = record.is_active
        secret = manager.decrypt_secret(record.encrypted_totp_secret)
        step = manager.get_matched_step(
            secret=secret,
            otp_code=otp,
            valid_window=1,
            last_accepted_step=record.last_accepted_step,
        )
        if step is None:
            raise ValueError("Invalid, expired, or previously used OTP")
        record.last_accepted_step = step
        if not already_active:
            record.is_active = True
        session.commit()
    print(f"OTP VALID: {application_code}:{code}")
    print("Status: ALREADY ACTIVE" if already_active else "Status: ACTIVATED")
    print("Note: this OTP step is now consumed for all team members.")


def regenerate_otp_qr(
    application_code: str,
    code: str,
    output: str = "otp_enrollment",
):
    """Export the EXISTING secret. Does not rotate or reset TOTP."""
    application_code, code = _normalize_otp_identity(application_code, code)
    qr_path = _get_otp_qr_path(application_code, code, output)
    if qr_path.exists():
        raise FileExistsError(f"QR file already exists: {qr_path}")
    if input(f"Export existing secret for {application_code}:{code}? Type EXPORT: ").strip() != "EXPORT":
        print("Cancelled.")
        return
    manager = _get_otp_manager()
    with OTPAuthenticator.managed_session(app_label="auth") as session:
        record = _get_authenticator(session, application_code, code)
        secret = manager.decrypt_secret(record.encrypted_totp_secret)
        name = record.name
    uri = manager.generate_provisioning_uri(secret, application_code, code)
    image = manager.generate_enrollment_qr(uri, secret, application_code, code, name)
    _save_otp_qr(image, qr_path)
    print(f"QR recovered: {qr_path}")
    print("Secret UNCHANGED. Keep image confidential and delete after enrollment.")


def rotate_otp_authenticator(
    application_code: str,
    code: str,
    output: str = "otp_enrollment",
):
    """Rotate shared secret, deactivate team, and revoke all form sessions.

    New enrollment image is prepared before DB update and then replaces
    the previous image. If replacement fails, the new image is retained.
    """
    application_code, code = _normalize_otp_identity(application_code, code)
    manager = _get_otp_manager()
    qr_path = _get_otp_qr_path(application_code, code, output)
    if input(f"Rotate {application_code}:{code}? Type ROTATE: ").strip() != "ROTATE":
        print("Cancelled.")
        return
    with OTPAuthenticator.managed_session(app_label="auth") as session:
        record = _get_authenticator(session, application_code, code)
        name = record.name
    enrollment = manager.create_enrollment(application_code, code)
    image = manager.generate_enrollment_qr(
        enrollment.provisioning_uri, enrollment.secret, application_code, code, name
    )
    temp_path = qr_path.with_name(f"{qr_path.stem}_{uuid.uuid4().hex}.png")
    _save_otp_qr(image, temp_path)
    try:
        with OTPAuthenticator.managed_session(app_label="auth") as session:
            record = _get_authenticator(session, application_code, code, lock=True)
            record.encrypted_totp_secret = manager.encrypt_secret(enrollment.secret)
            record.last_accepted_step = None
            record.is_active = False
            revoked_count = _revoke_sessions(session, record.id)
            session.commit()
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    try:
        os.replace(temp_path, qr_path)
    except OSError:
        print("Rotation committed; QR replacement failed.")
        print(f"New enrollment image retained at: {temp_path}")
        print("Old QR, if present, no longer generates valid codes.")
        raise
    print(f"Rotated: {application_code}:{code}")
    print(f"New enrollment image: {qr_path}")
    print(f"Status: INACTIVE; form sessions revoked: {revoked_count}")
    print("Scan the new QR and run verify_otp_authenticator to activate.")
    print("Delete obsolete copies of enrollment images; replacement is not secure erasure.")


def deactivate_otp_authenticator(application_code: str, code: str):
    """Administratively disable a team and revoke its form sessions.

    IMPORTANT: Current model has only is_active. The verify CLI can
    reactivate this record with a valid TOTP. To enforce admin-only
    reactivation, add a separate enrollment/status field in the model.
    """
    application_code, code = _normalize_otp_identity(application_code, code)
    if input(f"Deactivate {application_code}:{code}? Type DEACTIVATE: ").strip() != "DEACTIVATE":
        print("Cancelled.")
        return
    with OTPAuthenticator.managed_session(app_label="auth") as session:
        record = _get_authenticator(session, application_code, code, lock=True)
        record.is_active = False
        count = _revoke_sessions(session, record.id)
        session.commit()
    print(f"Deactivated: {application_code}:{code}; revoked sessions: {count}")
    print("Warning: verification with a valid OTP can reactivate this record.")
