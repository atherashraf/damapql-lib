"""
Authentication Router Module

This module provides authentication-related endpoints for login,
token refreshing, user verification, and user creation.
"""

import os
import traceback
from datetime import timedelta
from typing import List

from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Body, Form
from fastapi.security import OAuth2PasswordRequestForm
from jwt import ExpiredSignatureError, InvalidTokenError, DecodeError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette import status
from typing_extensions import Annotated
from pydantic import BaseModel, Field

from app.damapql.settings import ACCESS_TOKEN_EXPIRE_MINUTES
from app.damapql.utils.adapters.deps import get_sync_session
from app.damapql.utils.crypto import CryptoUtils

from .dtypes import UserInfo, UserCreate, UserUpdate
from .manager.email_manager import EmailUtils
from .manager.role_manager import UserRoleManager
from .models import Organization, Department, Profile, User, Role
from .resolvers import resolve_or_create_organization, resolve_or_create_department
from .utils import AuthUtils, oauth2_scheme
from ...wasa.models.admin import UserAOI


def clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None

auth_router = APIRouter(tags=["auth"])

INVALID_CREDENTIALS = "Incorrect username or password"


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh: str


class TokenVerifyRequest(BaseModel):
    token: str


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


@auth_router.post("/swagger")
def swagger_token_login(username: str = Form(...), password: str = Form(...)):
    user_info = User.get_user_info(username)
    if not user_info:
        da_logger.warning(f"Failed login: {username} does not exist.")
        raise HTTPException(status_code=400, detail=INVALID_CREDENTIALS)

    stored_password = User.get_password(username)
    if not stored_password or not AuthUtils.verify_password(password, stored_password):
        da_logger.warning(f"Failed login: invalid password for {username}.")
        raise HTTPException(status_code=400, detail=INVALID_CREDENTIALS)

    access_token = AuthUtils.create_access_token(username)

    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@auth_router.post("/jwt/login/json", response_model=AuthResponse)
async def login_json(data: LoginRequest, background_tasks: BackgroundTasks):
    return login_core(data.username, data.password, background_tasks)


@auth_router.post("/jwt/login", response_model=AuthResponse)
async def login_form(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    background_tasks: BackgroundTasks
):
    return login_core(form_data.username, form_data.password, background_tasks)


def login_core(username: str, password: str, background_tasks: BackgroundTasks) -> AuthResponse:
    user_info = User.get_user_info(username)
    if not user_info:
        da_logger.warning(f"Failed login: {username} does not exist.")
        raise HTTPException(status_code=400, detail=INVALID_CREDENTIALS)

    stored_password = User.get_password(username)
    if not stored_password or not AuthUtils.verify_password(password, stored_password):
        da_logger.warning(f"Failed login: invalid password for {username}.")
        raise HTTPException(status_code=400, detail=INVALID_CREDENTIALS)

    token = TokenResponse(
        accessToken=AuthUtils.create_access_token(username),
        refreshToken=AuthUtils.create_refresh_token(username),
        expiresIn=ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )

    background_tasks.add_task(User.update_login_status, username)
    return AuthResponse(token=token, userInfo=user_info)


@auth_router.post("/jwt/refresh_access_token/", response_model=TokenResponse)
async def get_access_token(info: RefreshTokenRequest):
    try:
        payload = AuthUtils.decode_token(info.refresh, is_refresh_token=True)
        username = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        user = User.get_user_info(username)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        access_token = AuthUtils.create_access_token(user.username)
        return TokenResponse(
            accessToken=access_token,
            refreshToken=info.refresh,
            expiresIn=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    except (ExpiredSignatureError, InvalidTokenError, DecodeError):
        da_logger.warning("Invalid refresh token received.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )


@auth_router.get("/jwt/user_info/")
async def get_user_info(token: Annotated[str, Depends(oauth2_scheme)]):
    AuthUtils.is_token_expire(token)
    user = AuthUtils.get_current_user(token)
    return {"user": user}


@auth_router.post("/jwt/token/verify")
async def verify_token(data: TokenVerifyRequest):
    try:
        AuthUtils.decode_token(data.token)
        return {"detail": "Token is valid"}
    except (ExpiredSignatureError, InvalidTokenError, DecodeError):
        da_logger.warning("Token verification failed.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )




@auth_router.post("/create_user", response_model=UserInfo)
def create_user(
    user_data: UserCreate,
    background_tasks: BackgroundTasks,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not current_user.is_superuser and not AuthUtils.has_role(current_user, "app_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to create users.")

    with User.managed_session() as session:
        if session.query(User).filter(User.username == user_data.username.strip().lower()).first():
            raise HTTPException(status_code=400, detail="Username already exists.")

        if session.query(Profile).filter(Profile.email == user_data.profile_email.strip().lower()).first():
            raise HTTPException(status_code=400, detail="Email already exists.")

        password = user_data.password if user_data.password else AuthUtils.generate_temp_password(8)

        try:
            created_user = AuthUtils.create_user(
                username=user_data.username.strip().lower(),
                password=password,
                email=user_data.profile_email.strip().lower(),
                name=user_data.profile_name.strip(),
                contact=clean_str(user_data.profile_contact),
                is_superuser=False,
                is_staff=True,
                roles=user_data.roles,
                organization=None,
                department=None,
                session=session,
            )

            if created_user.profile:
                created_user.profile.designation = clean_str(user_data.designation)

            if user_data.password:
                created_user.is_active = True
                created_user.is_verified = True

            session.flush()
            session.refresh(created_user)

            auth_user_id = created_user.id
            auth_username = created_user.username

            app_fe_url = os.environ.get("APP_FE_URL", "")

            if not user_data.password:
                token = AuthUtils.create_access_token(
                    subject=created_user.username,
                    expires_delta=timedelta(hours=1),
                )
                activation_url = f"{app_fe_url}/activateaccount?token={token}"

                email_subject, email_body = EmailUtils.get_email_subject_and_body(
                    email_type="create_pulse_user",
                    params={
                        "profile_name": user_data.profile_name,
                        "activation_url": activation_url,
                    },
                )

                background_tasks.add_task(
                    EmailUtils.send_email_background,
                    user_data.profile_email,
                    email_subject,
                    email_body,
                )
            else:
                email_subject, email_body = EmailUtils.get_email_subject_and_body(
                    email_type="user_created_with_password",
                    params={
                        "profile_name": user_data.profile_name,
                        "username": created_user.username,
                        "password": password,
                    },
                )

                background_tasks.add_task(
                    EmailUtils.send_email_background,
                    user_data.profile_email,
                    email_subject,
                    email_body,
                )

        except IntegrityError:
            session.rollback()
            traceback.print_exc()
            raise HTTPException(status_code=500, detail="Failed to create user.")
        except HTTPException:
            session.rollback()
            raise
        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to create user: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")

    # separate WASA DB write
    try:
        has_aoi_fields = any(
            value is not None
            for value in [
                user_data.dist_id,
                user_data.dist_name,
                user_data.wasa_div_id,
                user_data.wasa_div_name,
                user_data.wasa_subdiv_id,
                user_data.wasa_subdiv_name,
            ]
        )

        if has_aoi_fields:
            UserAOI.upsert_user_aoi(
                user_id=auth_user_id,
                username=auth_username,
                dist_id=user_data.dist_id,
                dist_name=user_data.dist_name,
                wasa_div_id=user_data.wasa_div_id,
                wasa_div_name=user_data.wasa_div_name,
                wasa_subdiv_id=user_data.wasa_subdiv_id,
                wasa_subdiv_name=user_data.wasa_subdiv_name,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"User created, but AOI sync failed: {str(exc)}",
        )

    with User.managed_session() as session:
        created_user = (
            session.query(User)
            .filter(User.id == auth_user_id)
            .first()
        )

        if not created_user:
            raise HTTPException(status_code=404, detail="User not found after creation.")

        return UserAOI.attach_user_aoi(created_user.to_object())





@auth_router.put("/update_user/{username}", response_model=UserInfo)
def update_user(
    username: str,
    user_data: UserUpdate,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not current_user.is_superuser and not AuthUtils.has_role(current_user, "app_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to update users.")

    with User.managed_session() as session:
        user = (
            session.query(User)
            .options(
                selectinload(User.profile),
                selectinload(User.roles),
            )
            .filter(User.username == username)
            .first()
        )

        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        if not user.profile:
            raise HTTPException(status_code=400, detail="User has no profile.")

        if user_data.profile_name is not None:
            user.profile.name = clean_str(user_data.profile_name)

        if user_data.profile_email is not None:
            normalized_email = user_data.profile_email.strip().lower()
            existing = (
                session.query(Profile)
                .filter(
                    Profile.email == normalized_email,
                    Profile.id != user.profile_id,
                )
                .first()
            )
            if existing:
                raise HTTPException(status_code=400, detail="Email already exists.")
            user.profile.email = normalized_email

        if user_data.profile_contact is not None:
            user.profile.contact = clean_str(user_data.profile_contact)

        if user_data.designation is not None:
            user.profile.designation = clean_str(user_data.designation)

        if user_data.password is not None:
            password = user_data.password.strip()
            if password:
                if len(password) < 6:
                    raise HTTPException(
                        status_code=400,
                        detail="Password must be at least 6 characters.",
                    )
                user.password = CryptoUtils().encrypt_txt(password)
                user.is_active = True
                user.is_verified = True

        if user_data.roles is not None:
            normalized_roles = [r.strip().lower() for r in user_data.roles]
            role_objects = (
                session.query(Role)
                .filter(Role.name.in_(normalized_roles), Role.is_active.is_(True))
                .all()
            )
            found_role_names = {role.name for role in role_objects}
            missing_roles = [role for role in normalized_roles if role not in found_role_names]

            if missing_roles:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid roles: {', '.join(missing_roles)}",
                )

            user.roles = role_objects

        user.save(session=session)
        session.refresh(user)

        auth_user_id = user.id
        auth_username = user.username

    try:
        UserAOI.upsert_user_aoi(
            user_id=auth_user_id,
            username=auth_username,
            dist_id=user_data.dist_id,
            dist_name=user_data.dist_name,
            wasa_div_id=user_data.wasa_div_id,
            wasa_div_name=user_data.wasa_div_name,
            wasa_subdiv_id=user_data.wasa_subdiv_id,
            wasa_subdiv_name=user_data.wasa_subdiv_name,
        )
    except Exception as exc:
        da_logger.exception("Error occurred while updating user's AOI.", exc)
        raise HTTPException(
            status_code=500,
            detail=f"User updated, but AOI sync failed: {str(exc)}",
        )

    with User.managed_session() as session:
        updated_user = (
            session.query(User)
            .options(
                selectinload(User.profile),
                selectinload(User.roles),
            )
            .filter(User.username == username)
            .first()
        )

        if not updated_user:
            raise HTTPException(status_code=404, detail="User not found after update.")

        return UserAOI.attach_user_aoi(updated_user.to_object())


@auth_router.delete("/delete_user/{username}")
def delete_user(
    username: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not current_user.is_superuser and not AuthUtils.has_role(current_user, "app_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to delete users.")

    normalized_username = username.strip().lower()
    if normalized_username == current_user.username:
        raise HTTPException(status_code=400, detail="You cannot delete your own user.")

    with User.managed_session() as session:
        user = (
            session.query(User)
            .options(selectinload(User.roles))
            .filter(User.username == normalized_username)
            .first()
        )

        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        if user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser accounts cannot be deleted.")

        user_id = user.id
        user.roles = []
        session.flush()
        session.delete(user)

    try:
        with UserAOI.managed_session() as session:
            user_aoi = session.query(UserAOI).filter(UserAOI.user_id == user_id).first()
            if user_aoi:
                session.delete(user_aoi)
    except Exception as exc:
        da_logger.exception("Error occurred while deleting user's AOI.")
        raise HTTPException(
            status_code=500,
            detail=f"User deleted, but AOI cleanup failed: {str(exc)}",
        )

    return {"detail": "User deleted successfully."}


@auth_router.post("/activate-account")
def activate_account(data: dict = Body(...), session: Session = Depends(get_sync_session)):
    token = data.get("token")
    new_password = data.get("password")

    try:
        payload = AuthUtils.decode_token(token)
        username = payload["sub"]

        user = session.query(User).filter_by(username=username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.is_verified:
            return {"message": "Account already activated"}

        user.password = CryptoUtils().encrypt_txt(new_password)
        user.is_verified = True
        user.is_active = True
        user.save(session=session)

        return {"message": "Account successfully activated"}

    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Activation link expired")
    except (InvalidTokenError, DecodeError):
        raise HTTPException(status_code=401, detail="Invalid activation link")
    except Exception as e:
        da_logger.exception(f"Failed to activate account: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to activate account: {str(e)}")


@auth_router.get("/user_list", response_model=UserListResponse)
def get_user_list(current_user: UserInfo = Depends(AuthUtils.get_current_user)) -> UserListResponse:
    with User.managed_session() as session:
        query = session.query(User).options(
            selectinload(User.profile).selectinload(Profile.organization),
            selectinload(User.profile).selectinload(Profile.department),
            selectinload(User.roles),
        ) #.where(User.id=='20')

        if current_user.is_superuser:
            query = query.filter(User.is_superuser.is_(False))

        elif AuthUtils.has_role(current_user, "app_manager"):
            query = query.join(User.roles).filter(
                Role.name.in_(["data_editor", "data_reviewer"])
            ).distinct()

        else:
            raise HTTPException(status_code=403, detail="Access denied")

        users = query.all()
        users_info = [UserAOI.attach_user_aoi(user.to_object()) for user in users]
        return UserListResponse(users=users_info)


@auth_router.post("/set_user_roles")
def set_user_roles(
    payload: SetRolesRequest,
    current_user: UserInfo = Depends(AuthUtils.get_current_user)
):
    if not (current_user.is_superuser or AuthUtils.has_role(current_user, "app_manager")):
        raise HTTPException(status_code=403, detail="Access denied")

    with User.managed_session() as session:
        user = (
            session.query(User)
            .options(
                selectinload(User.roles),
                selectinload(User.profile).selectinload(Profile.department),
            )
            .filter_by(username=payload.username)
            .first()
        )

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        UserRoleManager.set_roles(session, user, payload.roles)
        session.refresh(user)

        return {
            "username": user.username,
            "roles": [role.name for role in user.roles],
            "organization": user.profile.organization.name if user.profile and user.profile.organization else None,
            "department": user.profile.department.name if user.profile and user.profile.department else None,
            "is_verified": user.is_verified,
        }


class RoleListResponse(BaseModel):
    roles: List[str] = Field(default_factory=list)


class CreateRoleRequest(BaseModel):
    name: str
    description: str | None = None

@auth_router.get("/roles_list", response_model=RoleListResponse)
def get_roles_list(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    if not (
        current_user.is_superuser
        or AuthUtils.has_role(current_user, "app_manager")
    ):
        raise HTTPException(status_code=403, detail="Access denied")

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
            status_code=403,
            detail="Only superuser can create roles"
        )

    role_name = payload.name.strip().lower()

    with User.managed_session() as session:

        existing_role = (
            session.query(Role)
            .filter(Role.name == role_name)
            .first()
        )

        if existing_role:
            raise HTTPException(
                status_code=400,
                detail="Role already exists"
            )

        role = Role(
            name=role_name,
            description=payload.description,
            is_active=True,
        )

        session.add(role)
        session.commit()
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
