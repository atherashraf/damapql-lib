import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Union, Optional, List

import jwt
from digitalarzengine.utils.singletons import da_logger
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from typing_extensions import Annotated
from jwt import ExpiredSignatureError, InvalidTokenError

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.models import User, Profile, Role, Department, Organization
from app.damapql.utils.crypto import CryptoUtils
from app.damapql.settings import ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_MINUTES
from app.damapql.auth.user_aoi_service import (
    UserAOIService,
)

JWT_SECRET_KEY = os.environ["JWT_SECRET_KEY"]
JWT_REFRESH_SECRET_KEY = os.environ["JWT_REFRESH_SECRET_KEY"]
ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/swagger", scheme_name="JWT")
optional_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/auth/swagger",
    scheme_name="OptionalJWT",
    auto_error=False,
)

class AuthUtils:

    @staticmethod
    def create_access_token(subject: Union[str, Any], expires_delta: timedelta = None) -> str:
        expire = datetime.now(timezone.utc) + (
            expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        to_encode = {"exp": expire, "sub": str(subject)}
        return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def create_refresh_token(subject: Union[str, Any], expires_delta: timedelta = None) -> str:
        expire = datetime.now(timezone.utc) + (
            expires_delta if expires_delta else timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)
        )
        to_encode = {"exp": expire, "sub": str(subject)}
        return jwt.encode(to_encode, JWT_REFRESH_SECRET_KEY, algorithm=ALGORITHM)

    @classmethod
    def verify_password(cls, password: str, hashed_pass: str) -> bool:
        user_password = CryptoUtils().decrypt_txt(hashed_pass)
        return password == user_password

    @staticmethod
    def decode_token(token: str, is_refresh_token: bool = False) -> dict:
        secret_key = JWT_REFRESH_SECRET_KEY if is_refresh_token else JWT_SECRET_KEY
        return jwt.decode(token, secret_key, algorithms=[ALGORITHM])

    @staticmethod
    def is_token_expire(token: str, is_refresh_token: bool = False) -> bool:
        try:
            payload = AuthUtils.decode_token(token, is_refresh_token=is_refresh_token)
            expire_timestamp = payload["exp"]
            return expire_timestamp <= datetime.now(timezone.utc).timestamp()
        except ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired")
        except InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    @staticmethod
    def get_current_user(
            token: Annotated[
                str,
                Depends(oauth2_scheme),
            ],
    ) -> UserInfo:
        try:
            payload = AuthUtils.decode_token(token)
            username = payload.get("sub")

            if not username:
                da_logger.error(f"Invalid token payload: {payload}")
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token payload",
                )

            user = User.get_user_info(username)

            if not user:
                da_logger.error(f"User {username} not found")
                raise HTTPException(
                    status_code=401,
                    detail="User not found",
                )

            return UserAOIService.attach(user)

        except (
                ExpiredSignatureError,
                InvalidTokenError,
        ):
            da_logger.error("Could not validate credentials")
            raise HTTPException(
                status_code=401,
                detail="Could not validate credentials",
            )

    @classmethod
    def get_current_superuser(cls, user: Annotated[UserInfo, Depends(get_current_user)]) -> UserInfo:
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser privileges required")
        return user

    @staticmethod
    def create_user(
        username: str,
        password: str,
        email: str,
        name: str,
        contact: str = "",
        is_superuser: bool = False,
        is_staff: bool = False,
        is_active: Optional[bool] = None,
        is_verified: Optional[bool] = None,
        roles: Optional[List[str]] = None,
        organization: Optional[str] = None,
        department: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> User:
        roles = roles or []

        with User.managed_session(session) as s:
            organization_obj = None
            if organization:
                normalized_organization = organization.strip()
                organization_obj = (
                    s.query(Organization)
                    .filter(
                        Organization.name == normalized_organization,
                        Organization.is_active.is_(True)
                    )
                    .first()
                )
                if not organization_obj:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Organization '{organization}' not found"
                    )

            department_obj = None
            if department:
                normalized_department = department.strip().lower()
                query = s.query(Department).filter(
                    Department.name == normalized_department,
                    Department.is_active.is_(True)
                )

                if organization_obj:
                    query = query.filter(Department.organization_id == organization_obj.id)

                department_obj = query.first()
                if not department_obj:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Department '{department}' not found"
                    )

                if organization_obj and department_obj.organization_id != organization_obj.id:
                    raise HTTPException(
                        status_code=400,
                        detail="Department does not belong to the selected organization"
                    )

            if department_obj and not organization_obj:
                organization_obj = (
                    s.query(Organization)
                    .filter(Organization.id == department_obj.organization_id)
                    .first()
                )

            profile = Profile(
                name=name,
                email=email,
                contact=contact,
                organization_id=organization_obj.id if organization_obj else None,
                department_id=department_obj.id if department_obj else None,
            )
            saved_profile = profile.save(session=s)

            role_objects = []
            if roles:
                normalized_roles = [role.strip().lower() for role in roles]
                role_objects = (
                    s.query(Role)
                    .filter(Role.name.in_(normalized_roles), Role.is_active.is_(True))
                    .all()
                )

                found_role_names = {role.name for role in role_objects}
                missing_roles = [role for role in normalized_roles if role not in found_role_names]
                if missing_roles:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid roles: {', '.join(missing_roles)}"
                    )

            user = User(
                username=username,
                password=CryptoUtils().encrypt_txt(password),
                profile_id=saved_profile.id,
                is_superuser=is_superuser,
                is_staff=is_staff,
                is_active=is_superuser if is_active is None else is_active,
                is_verified=True if is_verified is None else is_verified,
            )
            s.add(user)
            s.flush()

            if role_objects:
                user.roles = role_objects

            user.save(session=s)
            return user

    @classmethod
    def create_token(cls, username: str, password: str) -> dict:
        with User.managed_session() as session:
            stmt = select(User).where(User.username == username)
            user = session.execute(stmt).scalar_one_or_none()

            if not user or not cls.verify_password(password, user.password):
                raise HTTPException(status_code=401, detail="Invalid username or password.")

            return {
                "access_token": cls.create_access_token(subject=user.username),
                "refresh_token": cls.create_refresh_token(subject=user.username),
                "token_type": "bearer",
            }

    @staticmethod
    def generate_temp_password(length: int = 10) -> str:
        characters = string.ascii_letters + string.digits + "!@#$%^&*()"
        return "".join(random.choices(characters, k=length))

    @staticmethod
    def update_user_roles(
        username: str,
        add_roles: Optional[List[str]] = None,
        remove_roles: Optional[List[str]] = None,
        session: Optional[Session] = None,
    ) -> bool:
        add_roles = [r.strip().lower() for r in (add_roles or [])]
        remove_roles = [r.strip().lower() for r in (remove_roles or [])]

        with User.managed_session(session) as s:
            user = (
                s.query(User)
                .options(selectinload(User.roles))
                .filter(User.username == username)
                .first()
            )

            if not user:
                raise ValueError(f"User '{username}' not found.")

            if add_roles:
                roles_to_add = (
                    s.query(Role)
                    .filter(Role.name.in_(add_roles), Role.is_active.is_(True))
                    .all()
                )

                found_role_names = {role.name for role in roles_to_add}
                missing_roles = [role for role in add_roles if role not in found_role_names]
                if missing_roles:
                    raise ValueError(f"Invalid roles: {', '.join(missing_roles)}")

                existing_role_names = {role.name for role in user.roles}
                for role in roles_to_add:
                    if role.name not in existing_role_names:
                        user.roles.append(role)

            if remove_roles:
                user.roles = [role for role in user.roles if role.name not in remove_roles]

            user.save(session=s)
            return True

    @staticmethod
    def update_user_department(
        username: str,
        department: Optional[str],
        organization: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> bool:
        with User.managed_session(session) as s:
            user = (
                s.query(User)
                .options(
                    selectinload(User.profile).selectinload(Profile.department),
                    selectinload(User.profile).selectinload(Profile.organization),
                )
                .filter(User.username == username)
                .first()
            )

            if not user:
                raise ValueError(f"User '{username}' not found.")

            if not user.profile:
                raise ValueError(f"User '{username}' has no profile.")

            if not department:
                user.profile.department_id = None
                user.save(session=s)
                return True

            normalized_department = department.strip().lower()
            query = s.query(Department).filter(
                Department.name == normalized_department,
                Department.is_active.is_(True)
            )

            organization_obj = None
            if organization:
                organization_obj = (
                    s.query(Organization)
                    .filter(
                        Organization.name == organization.strip(),
                        Organization.is_active.is_(True)
                    )
                    .first()
                )
                if not organization_obj:
                    raise ValueError(f"Organization '{organization}' not found.")

                query = query.filter(Department.organization_id == organization_obj.id)

            department_obj = query.first()

            if not department_obj:
                raise ValueError(f"Department '{department}' not found.")

            user.profile.department_id = department_obj.id

            if organization_obj:
                user.profile.organization_id = organization_obj.id
            elif not user.profile.organization_id:
                user.profile.organization_id = department_obj.organization_id

            user.save(session=s)
            return True

    @staticmethod
    def has_role(user: UserInfo, role: str) -> bool:
        if user.is_superuser:
            return True

        normalized_role = role.strip().lower()
        return normalized_role in [r.strip().lower() for r in user.roles]

    @staticmethod
    def require_role(required_role: str):
        def role_checker(user: UserInfo = Depends(AuthUtils.get_current_user)):
            if user.is_superuser:
                return user

            normalized_required_role = required_role.strip().lower()

            if normalized_required_role == "admin" and not user.is_superuser:
                raise HTTPException(status_code=403, detail="Admin privileges required")

            if normalized_required_role == "staff" and not user.is_staff:
                raise HTTPException(status_code=403, detail="Staff privileges required")

            user_role_values = [r.strip().lower() for r in user.roles]
            if normalized_required_role not in user_role_values:
                raise HTTPException(
                    status_code=403,
                    detail=f"Role '{required_role}' required"
                )

            return user

        return role_checker

    @staticmethod
    def require_department(required_department: str):
        def department_checker(user: UserInfo = Depends(AuthUtils.get_current_user)):
            if user.is_superuser:
                return user

            user_department = (user.department or "").strip().lower()
            normalized_required_department = required_department.strip().lower()

            if user_department != normalized_required_department:
                raise HTTPException(
                    status_code=403,
                    detail=f"Department '{required_department}' required"
                )

            return user

        return department_checker

    @staticmethod
    def has_roles(user: UserInfo, roles: List[str], require_all: bool = False) -> bool:
        if user.is_superuser:
            return True

        user_roles = {r.strip().lower() for r in user.roles}
        required_roles = {r.strip().lower() for r in roles}

        if require_all:
            return required_roles.issubset(user_roles)

        return bool(user_roles.intersection(required_roles))

    @staticmethod
    def get_optional_user(
            token: str | None = Depends(optional_oauth2_scheme),
    ) -> UserInfo | None:
        # da_logger.debug(f"Optional token: {token}")
        if not token:
            return None

        return AuthUtils.get_current_user(token)
