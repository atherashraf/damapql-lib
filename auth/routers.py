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
        if session.query(User).filter(User.username == user_data.username).first():
            raise HTTPException(status_code=400, detail="Username already exists.")

        if session.query(Profile).filter(Profile.email == user_data.profile_email).first():
            raise HTTPException(status_code=400, detail="Email already exists.")

        password = user_data.password if user_data.password else AuthUtils.generate_temp_password(8)

        try:
            created_user = AuthUtils.create_user(
                username=user_data.username,
                password=password,
                email=user_data.profile_email,
                name=user_data.profile_name,
                contact=user_data.profile_contact,
                is_superuser=False,
                is_staff=True,
                roles=user_data.roles,
                organization=user_data.organization,
                department=user_data.department,
                session=session,
            )

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
            user.profile.name = user_data.profile_name

        if user_data.profile_email is not None:
            existing = (
                session.query(Profile)
                .filter(
                    Profile.email == user_data.profile_email.strip().lower(),
                    Profile.id != user.profile_id
                )
                .first()
            )
            if existing:
                raise HTTPException(status_code=400, detail="Email already exists.")
            user.profile.email = user_data.profile_email

        if user_data.profile_contact is not None:
            user.profile.contact = user_data.profile_contact

        if user_data.designation is not None:
            user.profile.designation = user_data.designation

        # organization -> district
        # department -> subdivision
        organization_obj = None
        department_obj = None

        org_provided = user_data.organization is not None
        dept_provided = user_data.department is not None

        normalized_org = user_data.organization.strip() if org_provided and user_data.organization else ""
        normalized_dept = user_data.department.strip() if dept_provided and user_data.department else ""

        # Case 1: organization/district explicitly provided
        if org_provided:
            if normalized_org:
                organization_obj = (
                    session.query(Organization)
                    .filter(Organization.name == normalized_org)
                    .first()
                )

                if organization_obj:
                    if not organization_obj.is_active:
                        organization_obj.is_active = True
                else:
                    organization_obj = Organization(
                        name=normalized_org,
                        is_active=True,
                    )
                    session.add(organization_obj)
                    session.flush()  # get organization_obj.id
            else:
                # Explicit empty/null organization means clear organization
                organization_obj = None

        else:
            # organization not provided in payload -> keep current one
            organization_obj = user.profile.organization

        # Case 2: department/subdivision explicitly provided
        if dept_provided:
            if normalized_dept:
                # subdivision cannot be created without district because department.organization_id is required
                target_org = organization_obj

                if not target_org:
                    raise HTTPException(
                        status_code=400,
                        detail="Subdivision cannot be set without district."
                    )

                normalized_dept_lookup = normalized_dept.lower()

                department_obj = (
                    session.query(Department)
                    .filter(
                        Department.organization_id == target_org.id,
                        Department.name == normalized_dept_lookup,
                    )
                    .first()
                )

                if department_obj:
                    if not department_obj.is_active:
                        department_obj.is_active = True
                else:
                    department_obj = Department(
                        name=normalized_dept,
                        organization_id=target_org.id,
                        is_active=True,
                    )
                    session.add(department_obj)
                    session.flush()  # get department_obj.id
            else:
                # Explicit empty/null department means clear subdivision
                department_obj = None

        else:
            # department not provided in payload -> keep current one,
            # but only if organization still matches
            current_department = user.profile.department
            if current_department and organization_obj and current_department.organization_id == organization_obj.id:
                department_obj = current_department
            elif current_department and organization_obj is None:
                department_obj = None
            else:
                department_obj = current_department

        # Safety: if organization changed and existing department belongs to another org, clear it
        if department_obj and organization_obj:
            if department_obj.organization_id != organization_obj.id:
                department_obj = None

        if organization_obj is None:
            # no organization means no department
            department_obj = None

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