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
    current_user: UserInfo = Depends(AuthUtils.get_current_user)
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
            organization_obj = resolve_or_create_organization(
                session=session,
                organization_label=user_data.organization,
                dist_id=user_data.dist_id,
                dist_name=user_data.dist_name,
            )

            department_obj = resolve_or_create_department(
                session=session,
                organization_obj=organization_obj,
                department_label=user_data.department,
                subdiv_id=user_data.subdiv_id,
                subdiv_name=user_data.subdiv_name,
            )

            created_user = AuthUtils.create_user(
                username=user_data.username,
                password=password,
                email=user_data.profile_email,
                name=user_data.profile_name,
                contact=user_data.profile_contact,
                is_superuser=False,
                is_staff=True,
                roles=user_data.roles,
                organization=organization_obj.name if organization_obj else None,
                department=department_obj.name if department_obj else None,
                session=session,
            )

            if created_user.profile:
                created_user.profile.organization_id = organization_obj.id if organization_obj else None
                created_user.profile.department_id = department_obj.id if department_obj else None
                created_user.profile.designation = user_data.designation.strip() if user_data.designation else None

            session.flush()
            session.refresh(created_user)

            app_fe_url = os.environ.get("APP_FE_URL", "")

            if not user_data.password:
                token = AuthUtils.create_access_token(
                    subject=user_data.username,
                    expires_delta=timedelta(hours=1)
                )
                activation_url = f"{app_fe_url}/activateaccount?token={token}"

                email_subject, email_body = EmailUtils.get_email_subject_and_body(
                    email_type="create_pulse_user",
                    params={
                        "profile_name": user_data.profile_name,
                        "activation_url": activation_url,
                    }
                )

                background_tasks.add_task(
                    EmailUtils.send_email_background,
                    user_data.profile_email,
                    email_subject,
                    email_body
                )
            else:
                email_subject, email_body = EmailUtils.get_email_subject_and_body(
                    email_type="user_created_with_password",
                    params={
                        "profile_name": user_data.profile_name,
                        "username": user_data.username,
                        "password": password,
                    }
                )

                background_tasks.add_task(
                    EmailUtils.send_email_background,
                    user_data.profile_email,
                    email_subject,
                    email_body
                )

            return created_user.to_object()

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



@auth_router.put("/update_user/{username}", response_model=UserInfo)
def update_user(
    username: str,
    user_data: UserUpdate,
    current_user: UserInfo = Depends(AuthUtils.get_current_user)
):
    if not current_user.is_superuser and not AuthUtils.has_role(current_user, "app_manager"):
        raise HTTPException(status_code=403, detail="Not authorized to update users.")

    with User.managed_session() as session:
        user = (
            session.query(User)
            .options(
                selectinload(User.profile).selectinload(Profile.organization),
                selectinload(User.profile).selectinload(Profile.department),
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
            user.profile.name = user_data.profile_name.strip()

        if user_data.profile_email is not None:
            normalized_email = user_data.profile_email.strip().lower()
            existing = (
                session.query(Profile)
                .filter(Profile.email == normalized_email, Profile.id != user.profile_id)
                .first()
            )
            if existing:
                raise HTTPException(status_code=400, detail="Email already exists.")
            user.profile.email = normalized_email

        if user_data.profile_contact is not None:
            user.profile.contact = user_data.profile_contact.strip() if user_data.profile_contact else None

        if user_data.designation is not None:
            user.profile.designation = user_data.designation.strip() if user_data.designation else None

        has_any_location_field = any([
            user_data.organization is not None,
            user_data.department is not None,
            user_data.dist_id is not None,
            user_data.dist_name is not None,
            user_data.subdiv_id is not None,
            user_data.subdiv_name is not None,
        ])

        if has_any_location_field:
            # explicit clear all
            if (
                user_data.organization is None and
                user_data.department is None and
                user_data.dist_id is None and
                user_data.dist_name is None and
                user_data.subdiv_id is None and
                user_data.subdiv_name is None
            ):
                organization_obj = None
                department_obj = None
            else:
                organization_obj = resolve_or_create_organization(
                    session=session,
                    organization_label=user_data.organization,
                    dist_id=user_data.dist_id,
                    dist_name=user_data.dist_name,
                )

                department_obj = resolve_or_create_department(
                    session=session,
                    organization_obj=organization_obj,
                    department_label=user_data.department,
                    subdiv_id=user_data.subdiv_id,
                    subdiv_name=user_data.subdiv_name,
                )

            user.profile.organization_id = organization_obj.id if organization_obj else None
            user.profile.department_id = department_obj.id if department_obj else None

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
                    detail=f"Invalid roles: {', '.join(missing_roles)}"
                )
            user.roles = role_objects

        user.save(session=session)
        session.refresh(user)

        return user.to_object()


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
        )

        if current_user.is_superuser:
            query = query.filter(User.is_superuser.is_(False))

        elif AuthUtils.has_role(current_user, "app_manager"):
            query = query.join(User.roles).filter(
                Role.name.in_(["data_editor", "data_reviewer"])
            ).distinct()

        else:
            raise HTTPException(status_code=403, detail="Access denied")

        users = query.all()
        users_info = [user.to_object() for user in users]
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


for route in auth_router.routes:
    route.include_in_schema = False