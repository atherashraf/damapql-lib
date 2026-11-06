# app/damapql/config_registry/boundaries/router.py
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils

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


# app/damapql/config_registry/boundaries/router.py
@boundary_config_router.get("/options/")
def get_boundary_level_options(
    mode: str = Query(..., description="Registered mode key"),
    level: int = Query(..., description="Hierarchy level index (1, 2, 3...)"),
    parent_id: str | None = Query(None, description="Primary key of parent tier"),
    db: Session = Depends(get_sync_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    options = BoundaryHierarchyService.get_options(
        default_session=db,
        mode_key=mode,
        level=level,
        parent_id=parent_id,
    )
    return {"payload": options}