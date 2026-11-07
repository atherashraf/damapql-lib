# app/damapql/config_registry/boundaries/router.py
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.damapql.auth.auth_2FA_routers import get_session
from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.utils.adapters.db import DBBase

from app.damapql.utils.adapters.deps import get_sync_session
from app.damapql.config_registry.boundaries.schemas import BoundaryModeConfig
from app.damapql.config_registry.boundaries.service import BoundaryHierarchyService

boundary_config_router = APIRouter(prefix="/config/boundaries", tags=["config-boundaries"])



@boundary_config_router.get("/modes/", response_model=list[BoundaryModeConfig])
def get_boundary_modes(
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """Returns application-registered boundary modes."""
    return BoundaryHierarchyService.get_modes()





@boundary_config_router.get("/options/", summary="Get Boundary Level Options")
def get_boundary_options(
    mode: Optional[str] = Query(default=None, description="Boundary mode key"),
    mode_key: Optional[str] = Query(default=None, description="Boundary mode key alias"),
    level: int = Query(..., ge=1, description="Hierarchy level"),
    parent_id: Optional[str] = Query(default=None, description="Parent level primary key value"),
    # Use optional auth so unauthenticated requests don't fail with 422
    current_user: Optional[UserInfo] = Depends(
        getattr(AuthUtils, "get_optional_current_user", AuthUtils.get_current_user)
    ),
):
    resolved_mode = mode or mode_key
    if not resolved_mode:
        raise HTTPException(
            status_code=422,
            detail="Either 'mode' or 'mode_key' query parameter is required.",
        )
    options = BoundaryHierarchyService.get_options(
        mode_key=resolved_mode,
        level=level,
        parent_id=parent_id,
        current_user=current_user,
    )
    return {"payload": options}