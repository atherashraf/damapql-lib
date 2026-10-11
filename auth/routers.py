"""
Authentication Router Module

This module provides authentication-related endpoints for login,
token refreshing, account activation, user management, and role management.
"""

import os
from datetime import timedelta
from typing import List

from digitalarzengine.utils.singletons import da_logger
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
)
from fastapi.security import OAuth2PasswordRequestForm
from jwt import DecodeError, ExpiredSignatureError, InvalidTokenError
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette import status
from typing_extensions import Annotated

from app.damapql.auth.user_aoi_service import UserAOIService
from app.damapql.settings import ACCESS_TOKEN_EXPIRE_MINUTES
from app.damapql.utils.adapters.deps import get_sync_session
from app.damapql.utils.crypto import CryptoUtils

from .dtypes import UserCreate, UserInfo, UserUpdate
from .manager.email_manager import EmailUtils
from .manager.role_manager import UserRoleManager
from .models import Profile, Role, User
from .utils import AuthUtils, oauth2_scheme


auth_router = APIRouter(tags=["auth"])

INVALID_CREDENTIALS = "Incorrect username or password"
INACTIVE_ACCOUNT = "Account is inactive or not verified"


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()
    return value or None


def normalize_username(username: str) -> str:
    return username.strip().lower()


def ensure_login_allowed(user: UserInfo) -> None:
    if not user.is_active or not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INACTIVE_ACCOUNT,
        )


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh: str


class TokenVerifyRequest(BaseModel):
    token: str


class ActivateAccountRequest(BaseModel):
    token: str
    password: str


class TokenResponse(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int


class AuthResponse(BaseModel):
    token: TokenResponse
    userInfo: UserInfo


class UserListResponse(BaseModel):
    users: List[UserInfo] = Field(default_factory=list)


class SetRolesRequest(BaseModel):
    username: str
    roles: List[str] = Field(default_factory=list)


class RoleListResponse(BaseModel):
    roles: List[str] = Field(default_factory=list)


class CreateRoleRequest(BaseModel):
    name: str
    description: str | None = None


@auth_router.post("/swagger")
def swagger_token_login(
    username: str = Form(...),
    password: str = Form(...),
):
    normalized_username = normalize_username(username)
    user_info = User.get_user_info(normalized_username)

    if not user_info:
        da_logger.warning(
            f"Failed login: {normalized_username} does not exist."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CREDENTIALS,
        )

    stored_password = User.get_password(normalized_username)

    if (
        not stored_password
        or not AuthUtils.verify_password(password, stored_password)
    ):
        da_logger.warning(
            f"Failed login: invalid password for {normalized_username}."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CREDENTIALS,
        )

    ensure_login_allowed(user_info)

    return {
        "access_token": AuthUtils.create_access_token(normalized_username),
        "token_type": "bearer",
    }


@auth_router.post("/jwt/login/json", response_model=AuthResponse)
@auth_router.post("/jwt/login/json/", response_model=AuthResponse)
async def login_json(
    data: LoginRequest,
    background_tasks: BackgroundTasks,
) -> AuthResponse:
    return login_core(
        data.username,
        data.password,
        background_tasks,
    )


@auth_router.post("/jwt/login", response_model=AuthResponse)
async def login_form(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    background_tasks: BackgroundTasks,
) -> AuthResponse:
    return login_core(
        form_data.username,
        form_data.password,
        background_tasks,
    )


def login_core(
    username: str,
    password: str,
    background_tasks: BackgroundTasks,
) -> AuthResponse:
    normalized_username = normalize_username(username)
    user_info = User.get_user_info(normalized_username)

    if not user_info:
        da_logger.warning(
            f"Failed login: {normalized_username} does not exist."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CREDENTIALS,
        )

    stored_password = User.get_password(normalized_username)

    if (
        not stored_password
        or not AuthUtils.verify_password(password, stored_password)
    ):
        da_logger.warning(
            f"Failed login: invalid password for {normalized_username}."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_CREDENTIALS,
        )

    ensure_login_allowed(user_info)
    user_info = UserAOIService.attach(user_info)

    token = TokenResponse(
        accessToken=AuthUtils.create_access_token(normalized_username),
        refreshToken=AuthUtils.create_refresh_token(normalized_username),
        expiresIn=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

    background_tasks.add_task(
        User.update_login_status,
        normalized_username,
    )

    return AuthResponse(
        token=token,
        userInfo=user_info,
    )


@auth_router.post(
    "/jwt/refresh_access_token/",
    response_model=TokenResponse,
)
async def get_access_token(
    info: RefreshTokenRequest,
) -> TokenResponse:
    try:
        payload = AuthUtils.decode_token(
            info.refresh,
            is_refresh_token=True,
        )
        username = payload.get("sub")

        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        user = User.get_user_info(normalize_username(username))

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        ensure_login_allowed(user)

        return TokenResponse(
            accessToken=AuthUtils.create_access_token(user.username),
            refreshToken=info.refresh,
            expiresIn=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    except (ExpiredSignatureError, InvalidTokenError, DecodeError) as exc:
        da_logger.warning("Invalid refresh token received.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from exc


@auth_router.get("/jwt/user_info/")
async def get_user_info(
    token: Annotated[str, Depends(oauth2_scheme)],
):
    AuthUtils.is_token_expire(token)
    user = AuthUtils.get_current_user(token)
    return {"user": user}


@auth_router.post("/jwt/token/verify")
async def verify_token(data: TokenVerifyRequest):
    try:
        AuthUtils.decode_token(data.token)
        return {"detail": "Token is valid"}

    except (ExpiredSignatureError, InvalidTokenError, DecodeError) as exc:
        da_logger.warning("Token verification failed.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


@auth_router.post("/create_user", response_model=UserInfo)
def create_user(
    user_data: UserCreate,
    background_tasks: BackgroundTasks,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
) -> UserInfo:
    if (
        not current_user.is_superuser
        and not AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to create users.",
        )

    normalized_username = normalize_username(user_data.username)
    normalized_email = user_data.profile_email.strip().lower()
    normalized_name = user_data.profile_name.strip()

    if not normalized_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username is required.",
        )

    if not normalized_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile name is required.",
        )

    supplied_password = (
        user_data.password.strip()
        if user_data.password
        else None
    )
    password_was_provided = bool(supplied_password)

    if supplied_password and len(supplied_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters.",
        )

    password = supplied_password or AuthUtils.generate_temp_password(8)

    app_fe_url = os.environ.get("APP_FE_URL", "").rstrip("/")

    if password_was_provided:
        email_subject, email_body = EmailUtils.get_email_subject_and_body(
            email_type="user_created_with_password",
            params={
                "profile_name": normalized_name,
                "username": normalized_username,
                "password": password,
            },
        )
    else:
        activation_token = AuthUtils.create_access_token(
            subject=normalized_username,
            expires_delta=timedelta(hours=1),
        )
        activation_url = (
            f"{app_fe_url}/activateaccount?token={activation_token}"
        )
        email_subject, email_body = EmailUtils.get_email_subject_and_body(
            email_type="create_pulse_user",
            params={
                "profile_name": normalized_name,
                "activation_url": activation_url,
            },
        )

    try:
        with User.managed_session() as session:
            existing_user = (
                session.query(User)
                .filter(User.username == normalized_username)
                .first()
            )

            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username already exists.",
                )

            existing_profile = (
                session.query(Profile)
                .filter(Profile.email == normalized_email)
                .first()
            )

            if existing_profile:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already exists.",
                )

            created_user = AuthUtils.create_user(
                username=normalized_username,
                password=password,
                email=normalized_email,
                name=normalized_name,
                contact=clean_str(user_data.profile_contact),
                is_superuser=False,
                is_staff=True,
                is_active=password_was_provided,
                is_verified=password_was_provided,
                roles=user_data.roles,
                organization=None,
                department=None,
                session=session,
            )

            created_profile = getattr(
                created_user,
                "profile",
                None,
            )

            if created_profile is not None:
                created_profile.designation = clean_str(
                    user_data.designation
                )

            user_info = created_user.to_object()

            auth_user_id = user_info.id
            auth_username = user_info.username

    except IntegrityError as exc:
        da_logger.exception(f"Failed to create user: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user.",
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        da_logger.exception(f"Failed to create user: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {exc}",
        ) from exc

    try:
        UserAOIService.sync(
            user_id=auth_user_id,
            username=auth_username,
            payload=user_data,
        )
    except Exception as exc:
        da_logger.exception(
            f"User created, but AOI sync failed: {exc}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "User created, but AOI synchronization "
                f"failed: {exc}"
            ),
        ) from exc

    background_tasks.add_task(
        EmailUtils.send_email_background,
        normalized_email,
        email_subject,
        email_body,
    )

    return UserAOIService.attach(user_info)


@auth_router.put("/update_user/{username}", response_model=UserInfo)
def update_user(
    username: str,
    user_data: UserUpdate,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
) -> UserInfo:
    if (
        not current_user.is_superuser
        and not AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to update users.",
        )

    normalized_username = normalize_username(username)

    try:
        with User.managed_session() as session:
            user = (
                session.query(User)
                .options(
                    selectinload(User.profile).selectinload(
                        Profile.organization
                    ),
                    selectinload(User.profile).selectinload(
                        Profile.department
                    ),
                    selectinload(User.roles),
                )
                .filter(User.username == normalized_username)
                .first()
            )

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found.",
                )

            if not user.profile:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User has no profile.",
                )

            if user_data.profile_name is not None:
                profile_name = clean_str(user_data.profile_name)

                if not profile_name:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Profile name cannot be empty.",
                    )

                user.profile.name = profile_name

            if user_data.profile_email is not None:
                normalized_email = user_data.profile_email.strip().lower()
                existing_profile = (
                    session.query(Profile)
                    .filter(
                        Profile.email == normalized_email,
                        Profile.id != user.profile_id,
                    )
                    .first()
                )

                if existing_profile:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Email already exists.",
                    )

                user.profile.email = normalized_email

            if user_data.profile_contact is not None:
                user.profile.contact = clean_str(
                    user_data.profile_contact
                )

            if user_data.designation is not None:
                user.profile.designation = clean_str(
                    user_data.designation
                )

            if user_data.password is not None:
                password = user_data.password.strip()

                if password:
                    if len(password) < 6:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                "Password must be at least 6 characters."
                            ),
                        )

                    user.password = CryptoUtils().encrypt_txt(password)
                    user.is_active = True
                    user.is_verified = True

            if user_data.roles is not None:
                normalized_roles = [
                    role.strip().lower()
                    for role in user_data.roles
                ]
                role_objects = (
                    session.query(Role)
                    .filter(
                        Role.name.in_(normalized_roles),
                        Role.is_active.is_(True),
                    )
                    .all()
                    if normalized_roles
                    else []
                )

                found_role_names = {
                    role.name
                    for role in role_objects
                }
                missing_roles = [
                    role
                    for role in normalized_roles
                    if role not in found_role_names
                ]

                if missing_roles:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "Invalid roles: "
                            f"{', '.join(missing_roles)}"
                        ),
                    )

                user.roles = role_objects

            user.save(session=session)
            session.flush()

            auth_user_id = user.id
            auth_username = user.username
            user_info = user.to_object()

    except IntegrityError as exc:
        da_logger.exception(f"Failed to update user: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user.",
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        da_logger.exception(f"Failed to update user: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update user: {exc}",
        ) from exc

    try:
        UserAOIService.sync(
            user_id=auth_user_id,
            username=auth_username,
            payload=user_data,
        )
    except Exception as exc:
        da_logger.exception(
            f"User updated, but AOI sync failed: {exc}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "User updated, but AOI synchronization "
                f"failed: {exc}"
            ),
        ) from exc

    return UserAOIService.attach(user_info)


@auth_router.delete("/delete_user/{username}")
def deactivate_user(
    username: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if (
        not current_user.is_superuser
        and not AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete users.",
        )

    normalized_username = normalize_username(username)

    if normalized_username == current_user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own user.",
        )

    with User.managed_session() as session:
        user = (
            session.query(User)
            .filter(User.username == normalized_username)
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )

        if user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Superuser accounts cannot be deleted.",
            )

        user.is_active = False
        user.is_verified = False
        user.save(session=session)

    # This is a soft deactivation, so application AOI data is retained.
    return {"detail": "User deactivated successfully."}


@auth_router.post("/activate-account")
def activate_account(
    data: ActivateAccountRequest,
    session: Session = Depends(get_sync_session),
):
    password = data.password.strip()

    if len(password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters.",
        )

    try:
        payload = AuthUtils.decode_token(data.token)
        username = payload.get("sub")

        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid activation link",
            )

        user = (
            session.query(User)
            .filter(User.username == normalize_username(username))
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        if user.is_verified and user.is_active:
            return {"message": "Account already activated"}

        user.password = CryptoUtils().encrypt_txt(password)
        user.is_verified = True
        user.is_active = True
        user.save(session=session)

        return {"message": "Account successfully activated"}

    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Activation link expired",
        ) from exc

    except (InvalidTokenError, DecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid activation link",
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        da_logger.exception(f"Failed to activate account: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to activate account: {exc}",
        ) from exc


@auth_router.get("/user_list", response_model=UserListResponse)
def get_user_list(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
) -> UserListResponse:
    with User.managed_session() as session:
        query = (
            session.query(User)
            .options(
                selectinload(User.profile).selectinload(
                    Profile.organization
                ),
                selectinload(User.profile).selectinload(
                    Profile.department
                ),
                selectinload(User.roles),
            )
            .filter(User.is_active.is_(True))
        )

        if current_user.is_superuser:
            query = query.filter(User.is_superuser.is_(False))

        elif AuthUtils.has_role(current_user, "app_manager"):
            query = (
                query.join(User.roles)
                .filter(
                    Role.name.in_(["data_editor", "data_reviewer"])
                )
                .distinct()
            )

        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied",
            )

        users = query.all()
        users_info = [user.to_object() for user in users]

    return UserListResponse(
        users=[
            UserAOIService.attach(user_info)
            for user_info in users_info
        ]
    )


@auth_router.post("/set_user_roles")
def set_user_roles(
    payload: SetRolesRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not (
        current_user.is_superuser
        or AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    normalized_username = normalize_username(payload.username)

    with User.managed_session() as session:
        user = (
            session.query(User)
            .options(
                selectinload(User.roles),
                selectinload(User.profile).selectinload(
                    Profile.organization
                ),
                selectinload(User.profile).selectinload(
                    Profile.department
                ),
            )
            .filter(User.username == normalized_username)
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        UserRoleManager.set_roles(
            session,
            user,
            payload.roles,
        )
        session.flush()

        return {
            "username": user.username,
            "roles": [role.name for role in user.roles],
            "organization": (
                user.profile.organization.name
                if user.profile and user.profile.organization
                else None
            ),
            "department": (
                user.profile.department.name
                if user.profile and user.profile.department
                else None
            ),
            "is_verified": user.is_verified,
        }


@auth_router.get("/roles_list", response_model=RoleListResponse)
def get_roles_list(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
) -> RoleListResponse:
    if not (
        current_user.is_superuser
        or AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    with User.managed_session() as session:
        roles = (
            session.query(Role)
            .filter(Role.is_active.is_(True))
            .order_by(Role.name.asc())
            .all()
        )

        return RoleListResponse(
            roles=[role.name for role in roles]
        )


@auth_router.post("/create_role")
def create_role(
    payload: CreateRoleRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superuser can create roles",
        )

    role_name = payload.name.strip().lower()

    if not role_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role name is required",
        )

    with User.managed_session() as session:
        existing_role = (
            session.query(Role)
            .filter(Role.name == role_name)
            .first()
        )

        da_logger.debug(f"Session: {session}")
        da_logger.debug(f"Session bind: {session.get_bind()}")
        da_logger.debug(f"Engine URL: {session.get_bind().url.render_as_string(hide_password=True)}")
        da_logger.debug(f"Pool status: {session.get_bind().pool.status()}")

        if existing_role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Role already exists",
            )

        role = Role(
            name=role_name,
            description=clean_str(payload.description),
            is_active=True,
        )

        session.add(role)
        session.flush()
        session.refresh(role)

        return {
            "message": "Role created successfully",
            "role": {
                "id": role.id,
                "name": role.name,
                "description": role.description,
            },
        }


for route in auth_router.routes:
    route.include_in_schema = False
