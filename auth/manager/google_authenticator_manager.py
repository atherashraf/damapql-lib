
from __future__ import annotations

import base64
import hashlib
import io
import math
import re
import time
from dataclasses import dataclass

import pyotp
import qrcode

from cryptography.fernet import Fernet, InvalidToken
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class OTPEnrollment:
    """
    Enrollment information for a team authenticator.

    Treat every field as confidential because the QR code
    and provisioning URI contain the TOTP secret.
    """

    secret: str
    provisioning_uri: str
    qr_code_base64: str


class GoogleAuthenticatorManager:
    """
    Google Authenticator / TOTP utility for DAMapQL.

    Supports:
        - Team-shared TOTP enrollment
        - Fernet encryption of TOTP secrets
        - Google Authenticator provisioning URI
        - QR code generation
        - QR + manual setup key enrollment image
        - TOTP validation
        - Replay-aware TOTP time-step matching

    Application workflow:
        1. Administrator enrolls team authenticator.
        2. Administrator activates authenticator.
        3. User logs in to DAMapQL.
        4. User enters a fresh OTP to unlock a protected form.
        5. Authorization service creates a form session.
        6. User works until the form closes.
        7. Authorization service revokes the form session.

    IMPORTANT:
        This class does not manage database records,
        user sessions, permissions, or form access.

        Those responsibilities belong to
        OTPAuthorizationService.
    """

    ISSUER = "DAMapQL"

    INTERVAL = 30
    DIGITS = 6
    VALID_WINDOW = 1

    OTP_PATTERN = re.compile(r"^[0-9]{6}$")
    IDENTITY_PATTERN = re.compile(r"^[A-Z0-9_-]{2,32}$")

    def __init__(
        self,
        encryption_key: str,
        issuer: str = ISSUER,
    ):
        if not encryption_key:
            raise ValueError(
                "DAMapQL OTP encryption key is required"
            )

        try:
            self.fernet = Fernet(
                encryption_key.encode("utf-8")
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Invalid Fernet encryption key"
            ) from exc

        self.issuer = issuer.strip()

        if not self.issuer:
            raise ValueError(
                "TOTP issuer cannot be empty"
            )

    # ============================================================
    # Identity validation
    # ============================================================

    @classmethod
    def normalize_identity(
        cls,
        application_code: str,
        code: str,
    ) -> tuple[str, str]:

        if not isinstance(application_code, str):
            raise ValueError(
                "application_code must be a string"
            )

        if not isinstance(code, str):
            raise ValueError(
                "code must be a string"
            )

        application_code = application_code.strip().upper()
        code = code.strip().upper()

        if not cls.IDENTITY_PATTERN.fullmatch(application_code):
            raise ValueError(
                "Invalid application_code. "
                "Use 2-32 letters, numbers, underscores, or hyphens."
            )

        if not cls.IDENTITY_PATTERN.fullmatch(code):
            raise ValueError(
                "Invalid team code. "
                "Use 2-32 letters, numbers, underscores, or hyphens."
            )

        return application_code, code

    # ============================================================
    # Key generation
    # ============================================================

    @staticmethod
    def generate_encryption_key() -> str:
        """
        Generate a persistent Fernet encryption key.

        Generate once and store securely.

        Never regenerate this key during application startup.
        Existing encrypted secrets depend on it.
        """
        return Fernet.generate_key().decode("utf-8")

    @staticmethod
    def generate_secret() -> str:
        """
        Generate a new TOTP secret.

        Each team authenticator should have its own secret.
        """
        return pyotp.random_base32()

    # ============================================================
    # Secret validation and encryption
    # ============================================================

    @staticmethod
    def validate_secret(secret: str) -> str:
        """
        Validate and normalize a Base32 TOTP secret.
        """

        if not isinstance(secret, str):
            raise ValueError(
                "TOTP secret must be a string"
            )

        normalized = re.sub(
            r"\s+",
            "",
            secret,
        ).upper()

        if not normalized:
            raise ValueError(
                "TOTP secret cannot be empty"
            )

        try:
            base64.b32decode(
                normalized,
                casefold=True,
            )
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(
                "Invalid Base32 TOTP secret"
            ) from exc

        return normalized

    def encrypt_secret(self, secret: str) -> str:
        """
        Encrypt a TOTP secret for database storage.
        """

        secret = self.validate_secret(secret)

        return self.fernet.encrypt(
            secret.encode("utf-8")
        ).decode("utf-8")

    def decrypt_secret(self, encrypted_secret: str) -> str:
        """
        Decrypt a previously stored TOTP secret.
        """

        if not encrypted_secret:
            raise ValueError(
                "Encrypted TOTP secret is required"
            )

        try:
            decrypted = self.fernet.decrypt(
                encrypted_secret.encode("utf-8")
            ).decode("utf-8")

        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError(
                "Unable to decrypt TOTP secret"
            ) from exc

        return self.validate_secret(decrypted)

    # ============================================================
    # TOTP construction
    # ============================================================

    @classmethod
    def _create_totp(
            cls,
            secret: str,
    ) -> pyotp.TOTP:
        """
        Construct a TOTP generator using DAMapQL settings.

        Compatible with Google Authenticator:
            Algorithm : SHA1
            Digits    : 6
            Interval  : 30 seconds
        """

        return pyotp.TOTP(
            cls.validate_secret(secret),
            digits=cls.DIGITS,
            interval=cls.INTERVAL,
            digest=hashlib.sha1,
        )

    # ============================================================
    # Provisioning URI
    # ============================================================

    def generate_provisioning_uri(
        self,
        secret: str,
        application_code: str,
        code: str,
    ) -> str:
        """
        Generate an otpauth:// URI.

        Example:
            DAMapQL
            LBDC:PID
        """

        application_code, code = self.normalize_identity(
            application_code,
            code,
        )

        account_name = f"{application_code}:{code}"

        totp = self._create_totp(secret)

        return totp.provisioning_uri(
            name=account_name,
            issuer_name=self.issuer,
        )

    # ============================================================
    # QR code generation
    # ============================================================

    @staticmethod
    def _create_qr_image(
        provisioning_uri: str,
        box_size: int = 10,
    ) -> Image.Image:
        """
        Build the original QR image without resizing.

        Preserving module boundaries improves scan reliability.
        """

        if not provisioning_uri:
            raise ValueError(
                "Provisioning URI is required"
            )

        qr = qrcode.QRCode(
            version=None,
            error_correction=(
                qrcode.constants.ERROR_CORRECT_M
            ),
            box_size=box_size,
            border=4,
        )

        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        return qr.make_image(
            fill_color="black",
            back_color="white",
        ).convert("RGB")

    @staticmethod
    def _image_to_base64(
        image: Image.Image,
    ) -> str:
        """
        Encode a PIL image as base64 PNG.
        """

        buffer = io.BytesIO()

        image.save(
            buffer,
            format="PNG",
            optimize=True,
        )

        return base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")

    @classmethod
    def generate_qr_code(
        cls,
        provisioning_uri: str,
    ) -> str:
        """
        Return a base64-encoded QR PNG.

        Suitable for:
            data:image/png;base64,...
        """

        image = cls._create_qr_image(
            provisioning_uri=provisioning_uri,
            box_size=8,
        )

        return cls._image_to_base64(image)

    # ============================================================
    # Enrollment
    # ============================================================

    def create_enrollment(
        self,
        application_code: str,
        code: str,
    ) -> OTPEnrollment:
        """
        Create a new team TOTP enrollment package.

        IMPORTANT:
            Creating a new enrollment generates a NEW secret.

            For an existing authenticator, do not call this
            unless intentionally rotating the secret.

            Persist the encrypted secret before distributing
            the enrollment QR code.
        """

        application_code, code = self.normalize_identity(
            application_code,
            code,
        )

        secret = self.generate_secret()

        provisioning_uri = self.generate_provisioning_uri(
            secret=secret,
            application_code=application_code,
            code=code,
        )

        qr_code_base64 = self.generate_qr_code(
            provisioning_uri
        )

        return OTPEnrollment(
            secret=secret,
            provisioning_uri=provisioning_uri,
            qr_code_base64=qr_code_base64,
        )

    # ============================================================
    # Time-step utilities
    # ============================================================

    @classmethod
    def get_current_step(
        cls,
        at_time: float | None = None,
    ) -> int:
        """
        Return the current TOTP time step.
        """

        if at_time is None:
            at_time = time.time()

        if not math.isfinite(at_time) or at_time < 0:
            raise ValueError(
                "Invalid TOTP timestamp"
            )

        return int(at_time // cls.INTERVAL)

    @classmethod
    def get_remaining_seconds(
        cls,
        at_time: float | None = None,
    ) -> int:
        """
        Seconds remaining in the current TOTP interval.
        """

        if at_time is None:
            at_time = time.time()

        if not math.isfinite(at_time) or at_time < 0:
            raise ValueError(
                "Invalid TOTP timestamp"
            )

        remaining = cls.INTERVAL - (
            at_time % cls.INTERVAL
        )

        return math.ceil(remaining)

    # ============================================================
    # OTP validation
    # ============================================================

    @classmethod
    def normalize_otp(
        cls,
        otp_code: str,
    ) -> str | None:
        """
        Validate a six-digit OTP.

        Leading zeros are preserved.
        """

        if not isinstance(otp_code, str):
            return None

        otp_code = otp_code.strip()

        if not cls.OTP_PATTERN.fullmatch(otp_code):
            return None

        return otp_code

    @classmethod
    def get_matched_step(
        cls,
        secret: str,
        otp_code: str,
        valid_window: int = VALID_WINDOW,
        at_time: float | None = None,
        last_accepted_step: int | None = None,
    ) -> int | None:
        """
        Return the newest matching unused TOTP time step.

        Returns:
            int:
                Matching TOTP time step.

            None:
                Invalid, expired, or previously accepted OTP.

        Team-shared replay prevention:

            User A submits an OTP.
            The service records the accepted step.

            User B cannot submit the same accepted step.

            User B must wait for a newer acceptable OTP.

        IMPORTANT:
            The authorization service must use a database
            row lock when reading and updating
            OTPAuthenticator.last_accepted_step.

            This method alone cannot prevent concurrent
            requests from accepting the same OTP.
        """

        otp_code = cls.normalize_otp(otp_code)

        if otp_code is None:
            return None

        if (
            not isinstance(valid_window, int)
            or isinstance(valid_window, bool)
            or not 0 <= valid_window <= 2
        ):
            raise ValueError(
                "valid_window must be between 0 and 2"
            )

        if at_time is None:
            at_time = time.time()

        current_step = cls.get_current_step(at_time)

        totp = cls._create_totp(secret)

        matched_steps: list[int] = []

        for offset in range(
            -valid_window,
            valid_window + 1,
        ):
            candidate_step = current_step + offset

            if candidate_step < 0:
                continue

            # Prevent replay of an accepted step
            # or any earlier step.
            if (
                last_accepted_step is not None
                and candidate_step <= last_accepted_step
            ):
                continue

            expected_code = totp.at(
                candidate_step * cls.INTERVAL
            )

            if pyotp.utils.strings_equal(
                expected_code,
                otp_code,
            ):
                matched_steps.append(candidate_step)

        if not matched_steps:
            return None

        return max(matched_steps)

    @classmethod
    def verify_code(
        cls,
        secret: str,
        otp_code: str,
        valid_window: int = VALID_WINDOW,
    ) -> bool:
        """
        Basic TOTP validation.

        This does NOT persist replay-prevention state.

        For form unlocking, use get_matched_step()
        through OTPAuthorizationService.
        """

        return (
            cls.get_matched_step(
                secret=secret,
                otp_code=otp_code,
                valid_window=valid_window,
            )
            is not None
        )

    # ============================================================
    # Manual setup key formatting
    # ============================================================

    @staticmethod
    def _format_setup_key(
        secret: str,
        group_size: int = 4,
    ) -> str:
        """
        Format a TOTP secret for manual enrollment.

        Example:
            JBSWY3DPEHPK3PXP

        Becomes:
            JBSW Y3DP EHPK 3PXP
        """

        if group_size <= 0:
            raise ValueError(
                "group_size must be positive"
            )

        secret = GoogleAuthenticatorManager.validate_secret(
            secret
        )

        return " ".join(
            secret[i:i + group_size]
            for i in range(0, len(secret), group_size)
        )

    # ============================================================
    # Enrollment QR + manual setup key
    # ============================================================

    @classmethod
    def generate_enrollment_qr(
        cls,
        provisioning_uri: str,
        secret: str,
        application_code: str,
        code: str,
        name: str = "",
    ) -> str:
        """
        Generate a complete enrollment PNG containing:

            - DAMapQL heading
            - Application and team
            - Google Authenticator QR code
            - Manual setup key
            - Account information
            - TOTP settings
            - Confidentiality warning

        Returns:
            Base64-encoded PNG.

        WARNING:
            This image contains the team's TOTP secret.
            Anyone possessing it can generate valid OTPs.
        """

        application_code, code = cls.normalize_identity(
            application_code,
            code,
        )

        secret = cls.validate_secret(secret)

        account_name = f"{application_code}:{code}"

        # --------------------------------------------------------
        # QR image
        # --------------------------------------------------------

        qr_image = cls._create_qr_image(
            provisioning_uri=provisioning_uri,
            box_size=10,
        )

        # --------------------------------------------------------
        # Canvas
        # --------------------------------------------------------

        width = 640
        height = 850

        image = Image.new(
            "RGB",
            (width, height),
            "#FFFFFF",
        )

        draw = ImageDraw.Draw(image)

        # --------------------------------------------------------
        # Fonts
        # --------------------------------------------------------

        def load_font(
            size: int,
            bold: bool = False,
        ):
            if bold:
                candidates = [
                    "C:/Windows/Fonts/arialbd.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                ]
            else:
                candidates = [
                    "C:/Windows/Fonts/arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                ]

            for font_path in candidates:
                try:
                    return ImageFont.truetype(
                        font_path,
                        size,
                    )
                except OSError:
                    continue

            return ImageFont.load_default()

        title_font = load_font(30, bold=True)
        subtitle_font = load_font(23, bold=True)
        label_font = load_font(19)
        key_font = load_font(25, bold=True)
        small_font = load_font(16)

        # --------------------------------------------------------
        # Centered text helper
        # --------------------------------------------------------

        def centered_text(
            y: int,
            text: str,
            font,
            fill: str = "#111827",
        ):
            bbox = draw.textbbox(
                (0, 0),
                text,
                font=font,
            )

            text_width = bbox[2] - bbox[0]

            draw.text(
                ((width - text_width) / 2, y),
                text,
                font=font,
                fill=fill,
            )

        # --------------------------------------------------------
        # Header
        # --------------------------------------------------------

        draw.rectangle(
            (0, 0, width, 110),
            fill="#17365D",
        )

        centered_text(
            20,
            "DAMapQL",
            title_font,
            "#FFFFFF",
        )

        centered_text(
            65,
            "Google Authenticator Setup",
            label_font,
            "#E5E7EB",
        )

        # --------------------------------------------------------
        # Team information
        # --------------------------------------------------------

        centered_text(
            130,
            account_name,
            subtitle_font,
        )

        if name:
            centered_text(
                165,
                name,
                label_font,
                "#475569",
            )

        # --------------------------------------------------------
        # QR placement
        # --------------------------------------------------------

        # Scale only by an integer factor, if necessary.
        # Avoid arbitrary resizing that distorts QR modules.


        max_qr_size = 440

        if qr_image.width > max_qr_size:
            qr_image = qr_image.resize(
                (max_qr_size, max_qr_size),
                resample=Image.Resampling.NEAREST,
            )

        qr_x = (width - qr_image.width) // 2
        qr_y = 205 + (max_qr_size - qr_image.height) // 2

        image.paste(
            qr_image,
            (qr_x, qr_y),
        )

        # --------------------------------------------------------
        # Manual setup key
        # --------------------------------------------------------

        centered_text(
            660,
            "Manual Setup Key",
            subtitle_font,
        )

        formatted_key = cls._format_setup_key(secret)

        groups = formatted_key.split()

        midpoint = (len(groups) + 1) // 2

        key_lines = [
            " ".join(groups[:midpoint]),
            " ".join(groups[midpoint:]),
        ]

        draw.rounded_rectangle(
            (45, 695, 595, 770),
            radius=12,
            fill="#F1F5F9",
        )

        for index, line in enumerate(key_lines):
            if line:
                centered_text(
                    703 + index * 32,
                    line,
                    key_font,
                    "#0F172A",
                )

        # --------------------------------------------------------
        # Footer
        # --------------------------------------------------------

        centered_text(
            780,
            "Account: " + account_name,
            small_font,
        )

        centered_text(
            803,
            "Time-based (TOTP) | 6 digits | 30 seconds",
            small_font,
            "#475569",
        )

        centered_text(
            828,
            "CONFIDENTIAL - Delete after enrollment",
            small_font,
            "#B91C1C",
        )

        return cls._image_to_base64(image)
