# app/damapql/config_registry/workflows/router.py
from typing import List, Generator
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.auth.models import User
from app.damapql.config_registry.workflows.service import WorkflowEngineService
from app.damapql.config_registry.workflows.models import WorkflowInstance
from app.damapql.config_registry.workflows.schemas import (
    ExecuteTransitionRequest,
    WorkflowTransitionConfig,
    WorkflowAuditEntry,
)

workflow_config_router = APIRouter(prefix="/workflows", tags=["Workflow Engine"])


# Standard generator dependency for FastAPI
def get_session() -> Generator[Session, None, None]:
    with User.managed_session() as session:
        yield session


@workflow_config_router.get(
    "/instance/{instance_id}/allowed_actions/",
    response_model=List[WorkflowTransitionConfig],
)
def get_allowed_actions(
    instance_id: int,
    session: Session = Depends(get_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    instance = session.query(WorkflowInstance).filter_by(id=instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Workflow instance not found")
    return WorkflowEngineService.get_allowed_transitions(instance, current_user)


@workflow_config_router.post("/instance/{instance_id}/transition/")
def execute_transition(
    instance_id: int,
    payload: ExecuteTransitionRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    instance = WorkflowEngineService.execute_transition(
        session=session,
        instance_id=instance_id,
        req=payload,
        user=current_user,
        background_tasks=background_tasks,
    )
    session.commit()
    return {"success": True, "current_state": instance.current_state}


@workflow_config_router.get(
    "/instance/{instance_id}/history/",
    response_model=List[WorkflowAuditEntry],
)
def get_history(
    instance_id: int,
    session: Session = Depends(get_session),
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    instance = session.query(WorkflowInstance).filter_by(id=instance_id).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Workflow instance not found")

    return [
        WorkflowAuditEntry(
            id=log.id,
            from_state=log.from_state,
            to_state=log.to_state,
            action=log.action,
            performed_by_id=log.performed_by_id,
            performed_by_username=log.performed_by.username if log.performed_by else "system",
            comment=log.comment,
            timestamp=log.timestamp.isoformat(),
        )
        for log in instance.history
    ]