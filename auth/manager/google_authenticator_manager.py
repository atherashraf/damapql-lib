
from __future__ import annotations

import base64
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
    secret: str
    provisioning_uri: str
    qr_code_base64: str


class GoogleAuthenticatorManager:
    """
    Reusable TOTP utility for DAMapQL applications.

    Compatible with Google Authenticator and other
    standard TOTP authenticator applications.

    Responsibilities:
        - Generate TOTP secrets
        - Generate provisioning URIs
        - Generate QR codes
        - Encrypt/decrypt TOTP secrets
        - Verify 6-digit TOTP codes
        - Determine matched TOTP time steps

    Database operations, replay prevention, rate limiting,
    and authorization sessions belong in the service layer.
        manager = GoogleAuthenticatorManager(
            encryption_key=settings.DAMAPQL_OTP_ENCRYPTION_KEY
        )

        enrollment = manager.create_enrollment(
            application_code="LBDC",
            code="LUMS",
        )

        encrypted_secret = manager.encrypt_secret(
            enrollment.secret
        )
    """

    ISSUER = "DAMapQL"
    INTERVAL = 30
    DIGITS = 6
    VALID_WINDOW = 1

    def __init__(
        self,
        encryption_key: str,
        issuer: str = ISSUER,
    ):
        if not encryption_key:
            raise ValueError("OTP encryption key is required")

        self.fernet = Fernet(encryption_key.encode("utf-8"))
        self.issuer = issuer

    # ---------------------------------------------------------
    # Key generation
    # ---------------------------------------------------------

    @staticmethod
    def generate_encryption_key() -> str:
        """
        Generate a Fernet encryption key.

        Generate once, then store securely in environment
        variables or a secret manager.

        Do not regenerate on application startup.
        """
        return Fernet.generate_key().decode("utf-8")

    @staticmethod
    def generate_secret() -> str:
        """Generate a new cryptographically secure TOTP secret."""
        return pyotp.random_base32()

    # ---------------------------------------------------------
    # Encryption
    # ---------------------------------------------------------

    def encrypt_secret(self, secret: str) -> str:
        """Encrypt a TOTP secret before database storage."""
        return self.fernet.encrypt(
            secret.encode("utf-8")
        ).decode("utf-8")

    def decrypt_secret(self, encrypted_secret: str) -> str:
        """Decrypt a previously stored TOTP secret."""
        try:
            return self.fernet.decrypt(
                encrypted_secret.encode("utf-8")
            ).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Unable to decrypt TOTP secret"
            ) from exc

    # ---------------------------------------------------------
    # Provisioning URI
    # ---------------------------------------------------------

    def generate_provisioning_uri(
        self,
        secret: str,
        application_code: str,
        code: str,
    ) -> str:
        """
        Generate a URI compatible with Google Authenticator.

        Example account label:
            LBDC:LUMS
            LBDC:PU
            WASA:OPERATIONS
        """
        account_name = (
            f"{application_code.strip().upper()}:"
            f"{code.strip().upper()}"
        )

        totp = pyotp.TOTP(
            secret,
            digits=self.DIGITS,
            interval=self.INTERVAL,
        )

        return totp.provisioning_uri(
            name=account_name,
            issuer_name=self.issuer,
        )

    # ---------------------------------------------------------
    # QR generation
    # ---------------------------------------------------------

    @staticmethod
    def generate_qr_code(
        provisioning_uri: str,
    ) -> str:
        """
        Return a base64-encoded PNG QR code.

        Suitable for:
            <img src="data:image/png;base64,..." />
        """
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )

        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        image = qr.make_image(
            fill_color="black",
            back_color="white",
        )

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        return base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")

    # ---------------------------------------------------------
    # Enrollment
    # ---------------------------------------------------------

    def create_enrollment(
        self,
        application_code: str,
        code: str,
    ) -> OTPEnrollment:
        """
        Generate a new authenticator enrollment package.

        IMPORTANT:
        Persist the encrypted secret before returning
        the enrollment package to an authorized administrator.
        """
        secret = self.generate_secret()

        uri = self.generate_provisioning_uri(
            secret=secret,
            application_code=application_code,
            code=code,
        )

        return OTPEnrollment(
            secret=secret,
            provisioning_uri=uri,
            qr_code_base64=self.generate_qr_code(uri),
        )

    # ---------------------------------------------------------
    # TOTP utilities
    # ---------------------------------------------------------

    @classmethod
    def get_current_step(
        cls,
        at_time: float | None = None,
    ) -> int:
        if at_time is None:
            at_time = time.time()

        return int(at_time // cls.INTERVAL)

    @classmethod
    def get_remaining_seconds(
            cls,
            at_time: float | None = None,
    ) -> int:
        if at_time is None:
            at_time = time.time()

        return math.ceil(cls.INTERVAL - (at_time % cls.INTERVAL))

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
        Return the newest valid, unused TOTP time step.

        Args:
            secret:
                Decrypted TOTP secret.

            otp_code:
                Six-digit code from Google Authenticator.

            valid_window:
                Number of adjacent 30-second intervals
                accepted on either side of the current step.

            at_time:
                Optional Unix timestamp for verification.

            last_accepted_step:
                Last successfully accepted TOTP time step.
                Any matching step <= this value is rejected.

        Returns:
            int:
                Newest valid and unused TOTP time step.

            None:
                Invalid, expired, or previously used OTP.

        Note:
            The caller must read and update last_accepted_step
            atomically under a database row lock.
        """

        if not isinstance(otp_code, str):
            return None

        if not re.fullmatch(r"[0-9]{6}", otp_code):
            return None

        if not 0 <= valid_window <= 2:
            raise ValueError(
                "valid_window must be between 0 and 2"
            )

        if at_time is None:
            at_time = time.time()

        current_step = cls.get_current_step(at_time)

        totp = pyotp.TOTP(
            secret,
            digits=cls.DIGITS,
            interval=cls.INTERVAL,
        )

        matches: list[int] = []

        for offset in range(-valid_window, valid_window + 1):

            candidate_step = current_step + offset

            if candidate_step < 0:
                continue

            # Reject previously accepted time steps.
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
                matches.append(candidate_step)

        return max(matches) if matches else None


    @classmethod
    def verify_code(
        cls,
        secret: str,
        otp_code: str,
        valid_window: int = VALID_WINDOW,
    ) -> bool:
        """
        Basic TOTP verification.

        For authorization, use get_matched_step()
        together with database replay prevention.
        """
        return cls.get_matched_step(
            secret=secret,
            otp_code=otp_code,
            valid_window=valid_window,
        ) is not None

    @staticmethod
    def _format_setup_key(
            secret: str,
            group_size: int = 4,
    ) -> str:
        """
        Format a TOTP secret into readable groups.

        Example:
            JBSWY3DPEHPK3PXP
            -> JBSW Y3DP EHPK 3PXP
        """
        secret = secret.replace(" ", "").upper()

        return " ".join(
            secret[i:i + group_size]
            for i in range(0, len(secret), group_size)
        )

    @staticmethod
    def generate_enrollment_qr(
            provisioning_uri: str,
            secret: str,
            application_code: str,
            code: str,
            name: str = "",
    ) -> str:
        """
        Generate a complete Google Authenticator enrollment PNG.

        Includes:
            - DAMapQL heading
            - Application and team
            - QR code
            - Manual TOTP setup key
            - Account name
            - TOTP configuration
            - Confidentiality warning

        Returns:
            Base64-encoded PNG.

        IMPORTANT:
            Both QR and manual setup key contain the same
            sensitive TOTP secret.
        """

        application_code = application_code.strip().upper()
        code = code.strip().upper()

        account_name = f"{application_code}:{code}"

        # --------------------------------------------------------
        # Generate QR code
        # --------------------------------------------------------

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4,
        )

        qr.add_data(provisioning_uri)
        qr.make(fit=True)

        qr_image = qr.make_image(
            fill_color="black",
            back_color="white",
        ).convert("RGB")

        qr_image = qr_image.resize(
            (440, 440),
            Image.Resampling.NEAREST,
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

        def load_font(size: int, bold: bool = False):
            candidates = (
                [
                    "C:/Windows/Fonts/arialbd.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                ]
                if bold
                else [
                    "C:/Windows/Fonts/arial.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                ]
            )

            for font_path in candidates:
                try:
                    return ImageFont.truetype(font_path, size)
                except OSError:
                    continue

            return ImageFont.load_default()

        title_font = load_font(30, bold=True)
        subtitle_font = load_font(23, bold=True)
        label_font = load_font(19)
        key_font = load_font(25, bold=True)
        small_font = load_font(16)

        # --------------------------------------------------------
        # Helper: centered text
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
        # QR image
        # --------------------------------------------------------

        qr_x = (width - qr_image.width) // 2
        qr_y = 205

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

        formatted_key = (
            GoogleAuthenticatorManager._format_setup_key(secret)
        )

        # Wrap into two lines for longer TOTP secrets.
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

        # --------------------------------------------------------
        # Encode PNG
        # --------------------------------------------------------

        buffer = io.BytesIO()

        image.save(
            buffer,
            format="PNG",
            optimize=True,
        )

        return base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")
