import traceback
from datetime import datetime, timezone
from typing import Optional, List
from uuid import uuid4

import sqlalchemy as db
from sqlalchemy import select, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload, validates

from app.damapql.auth.dtypes import ProfileType, UserInfo
from app.damapql.utils.adapters.db import DBBase


user_role_association = db.Table(
    "auth_user_role",
    DBBase.metadata,
    db.Column("user_id", db.ForeignKey("auth_user.id"), primary_key=True),
    db.Column("role_id", db.ForeignKey("auth_role.id"), primary_key=True),
)


class Organization(DBBase):
    __tablename__ = "auth_organization"
    __table_args__ = (
        db.UniqueConstraint("dist_id", name="uq_auth_organization_dist_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(150), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

    # external district reference
    dist_id: Mapped[Optional[int]] = mapped_column(db.Integer, nullable=True, index=True)
    dist_name: Mapped[Optional[str]] = mapped_column(db.String(150), nullable=True)

    departments: Mapped[List["Department"]] = relationship(
        "Department",
        back_populates="organization",
        lazy="selectin",
    )

    profiles: Mapped[List["Profile"]] = relationship(
        "Profile",
        back_populates="organization",
        lazy="selectin",
    )

    def __repr__(self):
        return f"<Organization(name={self.name})>"

    @validates("name")
    def normalize_name(self, key, value):
        return value.strip()

    @validates("dist_name")
    def normalize_dist_name(self, key, value):
        return value.strip() if value else None

    @classmethod
    def get_id_by_name(cls, name: str) -> Optional[int]:
        normalized_name = name.strip()
        with cls.managed_session() as session:
            res = session.query(cls.id).filter(cls.name == normalized_name).first()
            return res[0] if res else None


class Department(DBBase):
    __tablename__ = "auth_department"
    __table_args__ = (
        db.UniqueConstraint("organization_id", "name", name="uq_department_org_name"),
        db.UniqueConstraint("subdiv_id", name="uq_auth_department_subdiv_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(100), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

    organization_id: Mapped[int] = mapped_column(
        db.ForeignKey("auth_organization.id"),
        nullable=False,
    )

    # external subdivision reference
    subdiv_id: Mapped[Optional[int]] = mapped_column(db.Integer, nullable=True, index=True)
    subdiv_name: Mapped[Optional[str]] = mapped_column(db.String(150), nullable=True)

    organization: Mapped["Organization"] = relationship(
        "Organization",
        back_populates="departments",
        lazy="selectin",
    )

    profiles: Mapped[List["Profile"]] = relationship(
        "Profile",
        back_populates="department",
        lazy="selectin",
    )

    @validates("name")
    def normalize_name(self, key, value):
        return value.strip().lower()

    @validates("subdiv_name")
    def normalize_subdiv_name(self, key, value):
        return value.strip() if value else None

    def __repr__(self):
        return f"<Department(name={self.name})>"


class Profile(DBBase):
    __tablename__ = "auth_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, nullable=False)
    name: Mapped[str] = mapped_column(db.String(150), nullable=False)
    email: Mapped[str] = mapped_column(db.String(255), nullable=False, unique=True, index=True)
    contact: Mapped[Optional[str]] = mapped_column(db.String(50), nullable=True)

    organization_id: Mapped[Optional[int]] = mapped_column(
        db.ForeignKey("auth_organization.id"),
        nullable=True,
    )

    department_id: Mapped[Optional[int]] = mapped_column(
        db.ForeignKey("auth_department.id"),
        nullable=True,
    )

    user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="profile",
        uselist=False,
    )

    organization: Mapped[Optional["Organization"]] = relationship(
        "Organization",
        back_populates="profiles",
        lazy="selectin",
    )

    department: Mapped[Optional["Department"]] = relationship(
        "Department",
        back_populates="profiles",
        lazy="selectin",
    )

    designation: Mapped[Optional[str]] = mapped_column(db.String(150), nullable=True)

    __pydantic__ = ProfileType

    def __repr__(self):
        return f"<Profile(name={self.name}, email={self.email})>"

    def to_object(self) -> ProfileType:
        return ProfileType(
            id=self.id,
            name=self.name,
            email=self.email,
            contact=self.contact,
            organization_id=self.organization_id,
            organization=self.organization.name if self.organization else None,
            department_id=self.department_id,
            department=self.department.name if self.department else None,
            dist_id=self.organization.dist_id if self.organization else None,
            dist_name=self.organization.dist_name if self.organization else None,
            subdiv_id=self.department.subdiv_id if self.department else None,
            subdiv_name=self.department.subdiv_name if self.department else None,
        )

    @classmethod
    def get_by_email(cls, email: str) -> Optional[ProfileType]:
        with cls.managed_session() as session:
            result = (
                session.query(cls)
                .options(
                    selectinload(cls.organization),
                    selectinload(cls.department),
                )
                .filter(cls.email == email)
                .first()
            )
            return result.to_object() if result else None

    @validates("email")
    def normalize_email(self, key, value):
        return value.strip().lower()


class Role(DBBase):
    __tablename__ = "auth_role"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(50), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

    users: Mapped[List["User"]] = relationship(
        "User",
        secondary=user_role_association,
        back_populates="roles",
        lazy="selectin",
    )

    @validates("name")
    def normalize_name(self, key, value):
        return value.strip().lower()

    def __repr__(self):
        return f"<Role(name={self.name})>"

    @classmethod
    def get_by_name(cls, name: str) -> Optional["Role"]:
        normalized_name = name.strip().lower()
        with cls.managed_session() as session:
            return session.query(cls).filter(cls.name == normalized_name).first()


class User(DBBase):
    __tablename__ = "auth_user"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, nullable=False)
    username: Mapped[str] = mapped_column(db.String(150), unique=True, nullable=False, index=True)
    password: Mapped[str] = mapped_column(db.String(255), nullable=False)

    profile_id: Mapped[Optional[int]] = mapped_column(
        db.ForeignKey("auth_profile.id"),
        unique=True,
        nullable=True,
    )

    is_superuser: Mapped[bool] = mapped_column(db.Boolean, default=False)
    is_staff: Mapped[bool] = mapped_column(db.Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(db.Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=False)

    last_login: Mapped[Optional[datetime]] = mapped_column(db.DateTime, nullable=True)
    date_joined: Mapped[datetime] = mapped_column(db.DateTime, nullable=False, default=datetime.utcnow)

    roles: Mapped[List[Role]] = relationship(
        "Role",
        secondary=user_role_association,
        back_populates="users",
        lazy="selectin",
    )

    profile: Mapped[Optional[Profile]] = relationship(
        "Profile",
        back_populates="user",
        uselist=False,
        lazy="selectin",
    )

    __pydantic__ = UserInfo

    @validates("username")
    def normalize_username(self, key, value):
        return value.strip().lower()

    @property
    def department(self) -> Optional["Department"]:
        return self.profile.department if self.profile else None

    @property
    def organization(self) -> Optional["Organization"]:
        return self.profile.organization if self.profile else None

    def to_object(self) -> UserInfo:
        profile = self.profile
        organization = self.organization
        department = self.department

        return UserInfo(
            id=self.id,
            username=self.username,
            is_superuser=self.is_superuser,
            is_staff=self.is_staff,
            is_active=self.is_active,
            is_verified=self.is_verified,
            roles=[
                role.name
                for role in self.roles
            ] if self.roles else [],
            organization=(
                organization.name
                if organization
                else None
            ),
            department=(
                department.name
                if department
                else None
            ),
            dist_id=(
                organization.dist_id
                if organization
                else None
            ),
            dist_name=(
                organization.dist_name
                if organization
                else None
            ),
            subdiv_id=(
                department.subdiv_id
                if department
                else None
            ),
            subdiv_name=(
                department.subdiv_name
                if department
                else None
            ),
            name=(
                profile.name
                if profile
                else None
            ),
            email=(
                profile.email
                if profile
                else None
            ),
            contact=(
                profile.contact
                if profile
                else None
            ),
            designation=(
                profile.designation
                if profile
                else None
            ),
        )

    def has_role(self, role_name: str) -> bool:
        normalized_role = role_name.strip().lower()
        return any(role.name == normalized_role for role in self.roles)

    @classmethod
    def is_user_exists(cls, username: str) -> bool:
        with cls.managed_session() as session:
            return session.query(cls).filter(cls.username == username).first() is not None

    @classmethod
    def get_by_profile_id(cls, profile_id: int) -> Optional[UserInfo]:
        with cls.managed_session() as session:
            result = (
                session.query(cls)
                .options(
                    selectinload(cls.roles),
                    selectinload(cls.profile).selectinload(Profile.organization),
                    selectinload(cls.profile).selectinload(Profile.department),
                )
                .filter(cls.profile_id == profile_id)
                .first()
            )
            return result.to_object() if result else None

    @classmethod
    def get_user(cls, username: str) -> Optional["User"]:
        with cls.managed_session() as session:
            user = (
                session.query(cls)
                .options(
                    selectinload(cls.roles),
                    selectinload(cls.profile).selectinload(Profile.organization),
                    selectinload(cls.profile).selectinload(Profile.department),
                )
                .filter(cls.username == username)
                .first()
            )
            return user

    @classmethod
    def get_user_info(
            cls,
            username: str,
    ) -> Optional[UserInfo]:
        with cls.managed_session() as session:
            user = (
                session.query(cls)
                .options(
                    selectinload(cls.roles),
                    selectinload(cls.profile)
                    .selectinload(Profile.organization),
                    selectinload(cls.profile)
                    .selectinload(Profile.department),
                )
                .filter(cls.username == username)
                .first()
            )

            return user.to_object() if user else None

    @classmethod
    def get_user_department(cls, username: str) -> Optional[str]:
        with cls.managed_session() as session:
            user = (
                session.query(cls)
                .options(
                    selectinload(cls.profile).selectinload(Profile.department)
                )
                .filter(cls.username == username)
                .first()
            )

            if not user or not user.profile or not user.profile.department:
                return None

            return user.profile.department.name

    @classmethod
    def get_password(cls, username: str) -> Optional[str]:
        query = select(cls.password).where(cls.username == username)
        with cls.managed_session() as session:
            res = session.execute(query).first()
            return str(res[0]) if res else None

    @classmethod
    def change_password(cls, username: str, new_password: str, confirm_password: str) -> bool:
        try:
            if new_password != confirm_password:
                return False

            with cls.managed_session() as session:
                user = session.query(cls).filter(cls.username == username).first()
                if not user:
                    return False

                user.password = new_password
                user.save(session=session)
                return True

        except Exception:
            traceback.print_exc()
            return False

    @classmethod
    def update_login_status(cls, username: str):
        with cls.managed_session() as session:
            user = session.query(cls).filter_by(username=username).first()
            if user:
                user.is_active = True
                user.last_login = datetime.utcnow()
                user.save(session=session)

    @classmethod
    def get_user_roles(cls, username: str) -> List[str]:
        with cls.managed_session() as session:
            user = (
                session.query(cls)
                .options(selectinload(cls.roles))
                .filter(cls.username == username)
                .first()
            )

            if not user:
                return []

            return [role.name for role in user.roles]





def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OTPAuthenticator(DBBase):
    """
    Team-level TOTP authenticator.

    One authenticator per (application_code, code).

    Example:
        application_code = "LBDC"
        code = "PID"

    Multiple users may share this authenticator.
    Each user receives an independent authorization session
    after successfully verifying an unused TOTP code.
    """

    __tablename__ = "otp_authenticators"

    __table_args__ = (UniqueConstraint("application_code", "code", name="uq_otp_authenticator_application_team", ),
                      db.Index("ix_otp_authenticator_application_active", "application_code", "is_active", ),)

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, autoincrement=True, )

    application_code: Mapped[str] = mapped_column(db.String(32), nullable=False, index=True, )

    code: Mapped[str] = mapped_column(db.String(32), nullable=False, index=True, )

    name: Mapped[str] = mapped_column(db.String(255), nullable=False, )

    # Encrypted with the persistent Fernet encryption key.
    encrypted_totp_secret: Mapped[str] = mapped_column(db.String(512), nullable=False, )

    # Example:
    # ["lbdc:advisory:tune", "lbdc:advisory:execute"]
    permissions: Mapped[list[str]] = mapped_column(db.JSON, nullable=False, default=list, )

    # Enrollment / administrative status.
    # Does NOT represent whether a form is currently open.
    is_active: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False, )

    # Prevents reuse of a TOTP time step across all team members.
    last_accepted_step: Mapped[Optional[int]] = mapped_column(db.BigInteger, nullable=True, )

    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now, )

    updated_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, )

    sessions: Mapped[list["OTPAuthorizationSession"]] = relationship("OTPAuthorizationSession",
        back_populates="authenticator", lazy="raise", )

    def __repr__(self) -> str:
        return (f"<OTPAuthenticator("
                f"id={self.id}, "
                f"application_code={self.application_code!r}, "
                f"code={self.code!r}, "
                f"is_active={self.is_active}"
                f")>")


class OTPAuthorizationSession(DBBase):
    """
    User-bound, form-scoped step-up authorization session.

    Lifecycle:
        OTP verified -> OPEN
        Form in use -> OPEN
        Form closed -> REVOKED
        Idle/max lifetime exceeded -> EXPIRED (derived)

    The backend must validate this session for protected
    operations, including RAM optimizer execution.
    """

    __tablename__ = "otp_authorization_sessions"

    __table_args__ = (db.Index("ix_otp_session_authenticator_revoked", "authenticator_id", "revoked", ),
                      db.Index("ix_otp_session_user_form", "user_id", "form_code", ),
                      db.Index("ix_otp_session_expires_at", "expires_at", ),)

    # UUID identifies the session record.
    id: Mapped[str] = mapped_column(db.String(64), primary_key=True, default=lambda: uuid4().hex, )

    authenticator_id: Mapped[int] = mapped_column(db.Integer,
        db.ForeignKey("otp_authenticators.id", ondelete="CASCADE", ), nullable=False, )

    # Store the authenticated DAMapQL user's ID.
    # Change this column type if your User model uses
    # a UUID or string primary key.
    user_id: Mapped[int] = mapped_column(db.Integer, nullable=False, index=True, )

    # Example: LBDC_RAM_OPTIMIZER
    form_code: Mapped[str] = mapped_column(db.String(100), nullable=False, )

    # SHA-256 hash of the random session token.
    # Never store the plaintext bearer token.
    token_hash: Mapped[str] = mapped_column(db.String(64), nullable=False, unique=True, )

    # Snapshot of permissions granted at unlock time.
    # Must also be checked against current authenticator
    # permissions to respect later administrative changes.
    permissions: Mapped[list[str]] = mapped_column(db.JSON, nullable=False, default=list, )

    created_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now, )

    last_activity_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, default=utc_now, )

    # Absolute maximum session lifetime.
    # The authorization service assigns this on creation.
    expires_at: Mapped[datetime] = mapped_column(db.DateTime(timezone=True), nullable=False, )

    # True when the form is explicitly closed or access
    # is administratively revoked.
    revoked: Mapped[bool] = mapped_column(db.Boolean, nullable=False, default=False, )

    revoked_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime(timezone=True), nullable=True, )

    authenticator: Mapped["OTPAuthenticator"] = relationship("OTPAuthenticator", back_populates="sessions",
        lazy="raise", )

    def is_valid(self, now: Optional[datetime] = None, idle_timeout_seconds: int = 1800, ) -> bool:
        """
        Local time/status check only.

        The authorization service must additionally verify:
        - Session token hash
        - Authenticated user identity
        - Form code
        - Required permission
        - Authenticator active status
        - Current authenticator permissions
        """
        now = now or utc_now()

        if self.revoked:
            return False

        if now >= self.expires_at:
            return False

        idle_seconds = (now - self.last_activity_at).total_seconds()

        if idle_seconds >= idle_timeout_seconds:
            return False

        return True

    def revoke(self) -> None:
        self.revoked = True
        self.revoked_at = utc_now()

    def touch(self) -> None:
        self.last_activity_at = utc_now()

    def __repr__(self) -> str:
        return (f"<OTPAuthorizationSession("
                f"id={self.id!r}, "
                f"user_id={self.user_id}, "
                f"form_code={self.form_code!r}, "
                f"revoked={self.revoked}"
                f")>")
