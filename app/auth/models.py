import traceback
from datetime import datetime
from typing import Optional, List

import sqlalchemy as db
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column, relationship, selectinload, validates

from app.auth.dtypes import ProfileType, UserInfo
from utils.adapters.db import DBBase


user_role_association = db.Table(
    "auth_user_role",
    DBBase.metadata,
    db.Column("user_id", db.ForeignKey("auth_user.id"), primary_key=True),
    db.Column("role_id", db.ForeignKey("auth_role.id"), primary_key=True),
)

class Organization(DBBase):
    __tablename__ = "auth_organization"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(150), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

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

    @validates("name")
    def normalize_name(self, key, value):
        return value.strip()

    def __repr__(self):
        return f"<Organization(name={self.name})>"


class Department(DBBase):
    __tablename__ = "auth_department"
    __table_args__ = (
        db.UniqueConstraint("organization_id", "name", name="uq_department_org_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(100), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

    organization_id: Mapped[int] = mapped_column(
        db.ForeignKey("auth_organization.id"),
        nullable=False,
    )

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
        nullable=True
    )

    department_id: Mapped[Optional[int]] = mapped_column(
        db.ForeignKey("auth_department.id"),
        nullable=True
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
        nullable=True
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

    def to_object(self) -> UserInfo:
        profile = self.profile
        return UserInfo(
            id=self.id,
            username=self.username,
            is_superuser=self.is_superuser,
            is_staff=self.is_staff,
            is_active=self.is_active,
            is_verified=self.is_verified,
            roles=[role.name for role in self.roles] if self.roles else [],
            organization=profile.organization.name if profile and profile.organization else None,
            department=profile.department.name if profile and profile.department else None,
            name=profile.name if profile else None,
            email=profile.email if profile else None,
            contact=profile.contact if profile else None,
        )