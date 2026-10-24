
"""
DAMapQL Google Authenticator CLI Management

Examples:

Generate encryption key:
    python -m app.damapql.manage generate_crypto_key
    set DAMAPQL_OTP_ENCRYPTION_KEY in .env  to proceed further
Create authenticator:
    python -m app.damapql.manage create_otp_authenticator \
        --application_code LBDC \
        --code PID \
        --name "PID PMIU Team" \
        --permissions "lbdc:advisory:tune,lbdc:advisory:execute"

Verify and activate:
    python -m app.damapql.manage verify_otp_authenticator \
        --application_code LBDC \
        --code PID

Show authenticator:
     python -m app.damapql.manage show_otp_authenticator --application_code LBDC \
     --code PID \
     --show_setup_key true

Recover QR from stored secret:
    python -m app.damapql.manage regenerate_otp_qr \
        --application_code LBDC \
        --code PID

change QR and stored key using rotate_otp_authenticator:
    python -m app.damapql.manage rotate_otp_authenticator \
     --application_code LBDC --code PID
"""

import base64
import getpass
import os
import re
from pathlib import Path

from app.damapql.auth.models import OTPAuthenticator
from app.damapql.auth.manager.google_authenticator_manager import (
    GoogleAuthenticatorManager,
)


# ============================================================
# OTP UTILITIES
# ============================================================

def _get_otp_manager() -> GoogleAuthenticatorManager:
    """
    Initialize the Google Authenticator manager using
    the persistent encryption key.
    """
    encryption_key = os.environ.get(
        "DAMAPQL_OTP_ENCRYPTION_KEY"
    )

    if not encryption_key:
        raise RuntimeError(
            "DAMAPQL_OTP_ENCRYPTION_KEY is not configured"
        )

    return GoogleAuthenticatorManager(
        encryption_key=encryption_key
    )


def _normalize_otp_identity(
    application_code: str,
    code: str,
) -> tuple[str, str]:
    """
    Normalize and validate application and team codes.
    """

    application_code = application_code.strip().upper()
    code = code.strip().upper()

    pattern = r"[A-Z0-9_-]{2,32}"

    if not re.fullmatch(pattern, application_code):
        raise ValueError(
            "Invalid application_code. "
            "Use 2-32 letters, numbers, underscores or hyphens."
        )

    if not re.fullmatch(pattern, code):
        raise ValueError(
            "Invalid team code. "
            "Use 2-32 letters, numbers, underscores or hyphens."
        )

    return application_code, code


def _parse_otp_permissions(
    permissions: str,
    application_code: str,
) -> list[str]:
    """
    Parse comma-separated application permissions.
    """

    permission_list = list(
        dict.fromkeys(
            permission.strip().lower()
            for permission in permissions.split(",")
            if permission.strip()
        )
    )

    if not permission_list:
        raise ValueError(
            "At least one permission is required"
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


def _get_otp_qr_path(
    application_code: str,
    code: str,
    output: str,
) -> Path:
    """
    Resolve the QR image path.
    """

    output_dir = Path(output).expanduser().resolve()

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

    return output_dir / (
        f"{application_code}_{code}_otp.png"
    )


def _save_otp_qr(
    qr_code_base64: str,
    qr_path: Path,
):
    """
    Save the QR PNG without overwriting existing files.

    POSIX: file is created with restricted permissions.
    Windows: directory/file ACLs must be secured separately.
    """

    qr_bytes = base64.b64decode(
        qr_code_base64,
        validate=True,
    )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL

    fd = os.open(
        qr_path,
        flags,
        0o600,
    )

    try:
        with os.fdopen(fd, "wb") as file:
            file.write(qr_bytes)

    except Exception:
        qr_path.unlink(missing_ok=True)
        raise

    return qr_path


# ============================================================
# CREATE OTP AUTHENTICATOR
# ============================================================

def create_otp_authenticator(
    application_code: str,
    code: str,
    name: str,
    permissions: str = "",
    output: str = "otp_enrollment",
):
    """
    Create a team OTP authenticator.

    Workflow:
        1. Validate team information.
        2. Generate a TOTP secret.
        3. Encrypt and save the secret in PostgreSQL.
        4. Generate a QR PNG.
        5. Keep authenticator inactive until verification.

    Existing authenticators are never overwritten.
    """

    application_code, code = _normalize_otp_identity(
        application_code,
        code,
    )

    name = name.strip()

    if not name:
        raise ValueError(
            "Authenticator name is required"
        )

    permission_list = _parse_otp_permissions(
        permissions,
        application_code,
    )

    manager = _get_otp_manager()

    qr_path = _get_otp_qr_path(
        application_code,
        code,
        output,
    )

    if qr_path.exists():
        raise FileExistsError(
            f"QR file already exists: {qr_path}"
        )

    # --------------------------------------------------------
    # Database enrollment
    # --------------------------------------------------------

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        existing = (
            session.query(OTPAuthenticator)
            .filter_by(
                application_code=application_code,
                code=code,
            )
            .first()
        )

        if existing:
            raise ValueError(
                f"Authenticator {application_code}:{code} "
                "already exists. No secret was changed."
            )

        enrollment = manager.create_enrollment(
            application_code=application_code,
            code=code,
        )

        authenticator = OTPAuthenticator(
            application_code=application_code,
            code=code,
            name=name,
            encrypted_totp_secret=manager.encrypt_secret(
                enrollment.secret
            ),
            permissions=permission_list,
            is_active=False,
            last_accepted_step=None,
        )

        session.add(authenticator)
        session.flush()

        authenticator_id = authenticator.id

        session.commit()

    # --------------------------------------------------------
    # Save QR image after successful DB commit
    # --------------------------------------------------------

    try:
        enrollment_qr_base64 = manager.generate_enrollment_qr(
            provisioning_uri=enrollment.provisioning_uri,
            secret=enrollment.secret,
            application_code=application_code,
            code=code,
            name=name,
        )

        _save_otp_qr(
            qr_code_base64=enrollment_qr_base64,
            qr_path=qr_path,
        )
    except Exception:
        print(
            "\nWARNING: Authenticator was saved, "
            "but QR generation failed."
        )
        print(
            "It remains inactive. Use regenerate_otp_qr "
            "to recover the QR."
        )
        raise

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("\n==========================================")
    print("DAMapQL Google Authenticator Enrollment")
    print("==========================================")
    print(f"Application : {application_code}")
    print(f"Team        : {code}")
    print(f"Name        : {name}")
    print(f"QR Code     : {qr_path}")

    print("\n--- Manual Setup ---")
    print(f"Account Name: {application_code}:{code}")
    print(f"Setup Key   : {enrollment.secret}")
    print("Key Type    : Time-based (TOTP)")
    print("Digits      : 6")
    print("Period      : 30 seconds")
    print("Issuer      : DAMapQL")

    print("\nIMPORTANT: Keep this setup key private.")
    print("Anyone possessing it can generate valid OTP codes.")
    print("==========================================")

    print("\nNext steps:")
    print("1. Open the generated QR PNG.")
    print("2. Scan it using Google Authenticator.")
    print("3. Run verify_otp_authenticator.")
    print("4. Securely delete the QR after enrollment.")


# ============================================================
# SHOW OTP AUTHENTICATOR
# ============================================================


def show_otp_authenticator(
    application_code: str,
    code: str,
    show_setup_key: str = "false",
):
    """
    Show authenticator metadata.

    Optionally display the existing TOTP Setup Key.
    Restrict this command to trusted administrators.
    """

    application_code, code = _normalize_otp_identity(
        application_code,
        code,
    )

    reveal_key = str(show_setup_key).strip().lower() in (
        "true", "1", "yes"
    )

    manager = _get_otp_manager() if reveal_key else None

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        authenticator = (
            session.query(OTPAuthenticator)
            .filter_by(
                application_code=application_code,
                code=code,
            )
            .first()
        )

        if authenticator is None:
            raise ValueError(
                f"Authenticator {application_code}:{code} "
                "was not found"
            )

        print("\n==========================================")
        print("DAMapQL OTP Authenticator")
        print("==========================================")
        print(f"ID          : {authenticator.id}")
        print(f"Application : {authenticator.application_code}")
        print(f"Team        : {authenticator.code}")
        print(f"Name        : {authenticator.name}")
        print(f"Active      : {authenticator.is_active}")
        print(f"Permissions : {authenticator.permissions}")
        print(f"Created     : {authenticator.created_at}")

        if reveal_key:
            confirmation = input(
                "\nReveal sensitive TOTP Setup Key? "
                "Type REVEAL to confirm: "
            )

            if confirmation == "REVEAL":
                secret = manager.decrypt_secret(
                    authenticator.encrypted_totp_secret
                )

                formatted_key = " ".join(
                    secret[i:i + 4]
                    for i in range(0, len(secret), 4)
                )

                print("\n--- Google Authenticator Manual Setup ---")
                print(f"Account Name: {application_code}:{code}")
                print(f"Setup Key   : {formatted_key}")
                print("Key Type    : Time-based (TOTP)")
                print("Digits      : 6")
                print("Period      : 30 seconds")
                print("Issuer      : DAMapQL")
                print("\nWARNING: Keep this setup key confidential.")
            else:
                print("Setup Key reveal cancelled.")

        print("==========================================")



# ============================================================
# VERIFY AND ACTIVATE OTP AUTHENTICATOR
# ============================================================

def verify_otp_authenticator(
    application_code: str,
    code: str,
):
    """
    Verify the Google Authenticator enrollment.

    A valid unused OTP activates the authenticator.

    Verification uses a database row lock to prevent
    concurrent acceptance of the same TOTP step.
    """

    application_code, code = _normalize_otp_identity(
        application_code,
        code,
    )

    manager = _get_otp_manager()

    otp_code = getpass.getpass(
        "Enter Google Authenticator 6-digit code: "
    )

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        authenticator = (
            session.query(OTPAuthenticator)
            .filter_by(
                application_code=application_code,
                code=code,
            )
            .with_for_update()
            .first()
        )

        if authenticator is None:
            raise ValueError(
                "Authenticator not found"
            )

        if authenticator.is_active:
            print(
                "Authenticator is already active."
            )
            return

        secret = manager.decrypt_secret(
            authenticator.encrypted_totp_secret
        )

        matched_step = manager.get_matched_step(
            secret=secret,
            otp_code=otp_code,
            valid_window=1,
            last_accepted_step=(
                authenticator.last_accepted_step
            ),
        )

        if matched_step is None:
            raise ValueError(
                "Invalid, expired, or previously used OTP"
            )

        authenticator.last_accepted_step = matched_step
        authenticator.is_active = True

        session.commit()

    print("\n==========================================")
    print("OTP Authenticator Activated")
    print("==========================================")
    print(f"Application : {application_code}")
    print(f"Team        : {code}")
    print("Status      : ACTIVE")
    print("==========================================")


# ============================================================
# REGENERATE QR FROM EXISTING SECRET
# ============================================================

def regenerate_otp_qr(
    application_code: str,
    code: str,
    output: str = "otp_enrollment",
):
    """
    Recover an enrollment QR using the existing secret.

    IMPORTANT:
        This does not rotate or change the TOTP secret.

        Restrict this CLI command to trusted administrators.

        QR files are never overwritten automatically.
    """

    application_code, code = _normalize_otp_identity(
        application_code,
        code,
    )

    manager = _get_otp_manager()

    qr_path = _get_otp_qr_path(
        application_code,
        code,
        output,
    )

    if qr_path.exists():
        raise FileExistsError(
            f"QR file already exists: {qr_path}"
        )

    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        authenticator = (
            session.query(OTPAuthenticator)
            .filter_by(
                application_code=application_code,
                code=code,
            )
            .first()
        )

        if authenticator is None:
            raise ValueError(
                "Authenticator not found"
            )

        # Recover the original TOTP secret.
        secret = manager.decrypt_secret(
            authenticator.encrypted_totp_secret
        )

        provisioning_uri = manager.generate_provisioning_uri(
            secret=secret,
            application_code=application_code,
            code=code,
        )

        qr_code_base64 = manager.generate_enrollment_qr(
            provisioning_uri=provisioning_uri,
            secret=secret,
            application_code=application_code,
            code=code,
            name=authenticator.name,
        )

    # Explicit confirmation before exposing a secret.
    confirmation = input(
        f"Export secret for {application_code}:{code}? "
        "Type EXPORT to confirm: "
    )

    if confirmation != "EXPORT":
        print("QR export cancelled.")
        return

    _save_otp_qr(
        qr_code_base64=qr_code_base64,
        qr_path=qr_path,
    )

    print("\n==========================================")
    print("OTP QR Recovered")
    print("==========================================")
    print(f"Application : {application_code}")
    print(f"Team        : {code}")
    print(f"QR Code     : {qr_path}")
    print("Secret      : UNCHANGED")
    print("==========================================")
    print(
        "WARNING: This QR grants access to the "
        "existing team authenticator."
    )
    print(
        "Delete it securely after enrollment."
    )



def rotate_otp_authenticator(
    application_code: str,
    code: str,
    output: str = "otp_enrollment",
):
    """
    Rotate an existing team's TOTP secret.

    Workflow:
        1. Confirm rotation.
        2. Generate a new TOTP secret.
        3. Generate a new enrollment QR with Setup Key.
        4. Save new QR to a temporary unique filename.
        5. Update encrypted secret in PostgreSQL.
        6. Revoke existing OTP authorization sessions.
        7. Delete the old QR image.
        8. Keep authenticator inactive until verified.

    Note:
        Old QR images copied elsewhere cannot be
        automatically deleted, but their OTP secrets
        become invalid after rotation.
    """

    import uuid

    from app.damapql.auth.models import (
        OTPAuthorizationSession,
    )

    application_code, code = _normalize_otp_identity(
        application_code,
        code,
    )

    manager = _get_otp_manager()

    # Existing QR image
    old_qr_path = _get_otp_qr_path(
        application_code,
        code,
        output,
    )

    confirmation = input(
        f"\nRotate OTP secret for {application_code}:{code}? "
        "Type ROTATE to confirm: "
    )

    if confirmation != "ROTATE":
        print("Rotation cancelled.")
        return

    # --------------------------------------------------------
    # Generate new secret and enrollment QR
    # --------------------------------------------------------

    enrollment = manager.create_enrollment(
        application_code=application_code,
        code=code,
    )

    # Temporary unique filename; never overwrite the old QR
    temp_qr_path = old_qr_path.with_name(
        f"{old_qr_path.stem}_{uuid.uuid4().hex[:8]}.png"
    )

    # Read team metadata for the enrollment card
    with OTPAuthenticator.managed_session(
        app_label="auth"
    ) as session:

        authenticator = (
            session.query(OTPAuthenticator)
            .filter_by(
                application_code=application_code,
                code=code,
            )
            .first()
        )

        if authenticator is None:
            raise ValueError("Authenticator not found")

        team_name = authenticator.name

    enrollment_qr_base64 = manager.generate_enrollment_qr(
        provisioning_uri=enrollment.provisioning_uri,
        secret=enrollment.secret,
        application_code=application_code,
        code=code,
        name=team_name,
    )

    # Save new QR before database changes
    _save_otp_qr(
        qr_code_base64=enrollment_qr_base64,
        qr_path=temp_qr_path,
    )

    # --------------------------------------------------------
    # Rotate secret in database
    # --------------------------------------------------------

    try:
        with OTPAuthenticator.managed_session(
            app_label="auth"
        ) as session:

            authenticator = (
                session.query(OTPAuthenticator)
                .filter_by(
                    application_code=application_code,
                    code=code,
                )
                .with_for_update()
                .first()
            )

            if authenticator is None:
                raise ValueError("Authenticator not found")

            authenticator.encrypted_totp_secret = (
                manager.encrypt_secret(enrollment.secret)
            )

            authenticator.last_accepted_step = None
            authenticator.is_active = False

            # Revoke existing OTP authorization sessions
            (
                session.query(OTPAuthorizationSession)
                .filter(
                    OTPAuthorizationSession.authenticator_id
                    == authenticator.id
                )
                .update(
                    {OTPAuthorizationSession.revoked: True},
                    synchronize_session=False,
                )
            )

            session.commit()

    except Exception:
        temp_qr_path.unlink(missing_ok=True)
        raise

    # --------------------------------------------------------
    # Replace old enrollment QR
    # --------------------------------------------------------

    # Replace the old image with the newly generated one.
    # os.replace performs an atomic replacement on the
    # same filesystem where supported.
    try:
        os.replace(
            temp_qr_path,
            old_qr_path,
        )
    except OSError as exc:
        print(
            "\nWARNING: Secret rotation succeeded, "
            "but QR replacement failed."
        )
        print(f"New QR retained at: {temp_qr_path}")
        print(f"Error: {exc}")
        print(
            "The old QR is invalid but may still exist. "
            "Remove it manually."
        )
        raise

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("\n==========================================")
    print("OTP AUTHENTICATOR SECRET ROTATED")
    print("==========================================")
    print(f"Application : {application_code}")
    print(f"Team        : {code}")
    print("Status      : INACTIVE")
    print("Old OTP     : INVALID")
    print("OTP Sessions: REVOKED")
    print(f"New QR      : {old_qr_path}")
    print("==========================================")

    print("\nNext steps:")
    print("1. Open the new enrollment QR image.")
    print("2. Scan the QR or enter the new Setup Key.")
    print("3. Run verify_otp_authenticator.")
    print("4. Remove any other copies of old enrollment images.")
