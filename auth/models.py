import traceback
from datetime import datetime
from typing import Optional, List

import sqlalchemy as db
from sqlalchemy import select
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
            roles=[role.name for role in self.roles] if self.roles else [],
            organization=organization.name if organization else None,
            department=department.name if department else None,
            dist_id=organization.dist_id if organization else None,
            dist_name=organization.dist_name if organization else None,
            subdiv_id=department.subdiv_id if department else None,
            subdiv_name=department.subdiv_name if department else None,
            name=profile.name if profile else None,
            email=profile.email if profile else None,
            contact=profile.contact if profile else None,
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
    def get_user_info(cls, username: str) -> Optional[UserInfo]:
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

            if not user:
                return None

            return user.to_object()

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