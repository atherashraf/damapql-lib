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


class Profile(DBBase):
    __tablename__ = "auth_profile"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, nullable=False)
    name: Mapped[str] = mapped_column(db.String(150), nullable=False)
    email: Mapped[str] = mapped_column(db.String(255), nullable=False, unique=True, index=True)
    contact: Mapped[Optional[str]] = mapped_column(db.String(50), nullable=True)

    user = relationship("User", back_populates="profile", uselist=False)

    __pydantic__ = ProfileType

    def to_object(self) -> ProfileType:
        return ProfileType(
            id=self.id,
            name=self.name,
            email=self.email,
            contact=self.contact,
        )

    @classmethod
    def get_by_email(cls, email: str) -> Optional[ProfileType]:
        with cls.managed_session() as session:
            result = session.query(cls).filter(cls.email == email).first()
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


class Department(DBBase):
    __tablename__ = "auth_department"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(db.String(100), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(db.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(db.Boolean, default=True)

    users: Mapped[List["User"]] = relationship(
        "User",
        back_populates="department",
        lazy="selectin",
    )

    @validates("name")
    def normalize_name(self, key, value):
        return value.strip().lower()

    def __repr__(self):
        return f"<Department(name={self.name})>"

    @classmethod
    def get_by_name(cls, name: str) -> Optional["Department"]:
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

    department_id: Mapped[Optional[int]] = mapped_column(
        db.ForeignKey("auth_department.id"),
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

    department: Mapped[Optional[Department]] = relationship(
        "Department",
        back_populates="users",
        lazy="selectin",
    )

    __pydantic__ = UserInfo

    def __repr__(self):
        return (
            f"<User(username={self.username}, "
            f"department={self.department.name if self.department else None}, "
            f"roles={[r.name for r in self.roles]})>"
        )

    def to_object(self) -> UserInfo:
        return UserInfo(
            id=self.id,
            username=self.username,
            is_superuser=self.is_superuser,
            is_staff=self.is_staff,
            is_active=self.is_active,
            is_verified=self.is_verified,
            roles=[role.name for role in self.roles] if self.roles else [],
            department=self.department.name if self.department else None,
            name=self.profile.name if self.profile else None,
            email=self.profile.email if self.profile else None,
            contact=self.profile.contact if self.profile else None,
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
                    selectinload(cls.profile),
                    selectinload(cls.roles),
                    selectinload(cls.department),
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
                    selectinload(cls.profile),
                    selectinload(cls.roles),
                    selectinload(cls.department),
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
                    selectinload(cls.profile),
                    selectinload(cls.roles),
                    selectinload(cls.department),
                )
                .filter(cls.username == username)
                .first()
            )

            if not user:
                return None

            return user.to_object()

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

    @classmethod
    def get_user_department(cls, username: str) -> Optional[str]:
        with cls.managed_session() as session:
            user = (
                session.query(cls)
                .options(selectinload(cls.department))
                .filter(cls.username == username)
                .first()
            )

            if not user or not user.department:
                return None

            return user.department.name