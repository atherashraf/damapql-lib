# app/damapql/config_registry/workflows/service.py
from typing import List, Optional
from fastapi import HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.auth.manage.manage_google_authenticator import (
    _get_otp_manager,
    _get_authenticator,
    _normalize_otp_identity,
)
from app.damapql.auth.manager.email_manager import EmailUtils
from app.damapql.config_registry.workflows.base import BaseWorkflowRegistry
from app.damapql.config_registry.workflows.schemas import (
    WorkflowTransitionConfig,
    ExecuteTransitionRequest,
)
from app.damapql.config_registry.workflows.models import (
    WorkflowInstance,
    WorkflowAuditLog,
)


class WorkflowEngineService:

    @classmethod
    def start_workflow(
        cls,
        session: Session,
        workflow_key: str,
        entity_type: str,
        entity_id: int,
        creator: UserInfo,
        dist_id: Optional[int] = None,
        subdiv_id: Optional[int] = None,
    ) -> WorkflowInstance:
        wf_def = BaseWorkflowRegistry.get_workflow(workflow_key)
        if not wf_def:
            raise HTTPException(status_code=400, detail=f"Workflow '{workflow_key}' is not registered.")

        initial_state = next((s.key for s in wf_def.states if s.is_initial), wf_def.states[0].key)

        instance = WorkflowInstance(
            workflow_key=workflow_key,
            entity_type=entity_type,
            entity_id=entity_id,
            current_state=initial_state,
            dist_id=dist_id,
            subdiv_id=subdiv_id,
            created_by_id=creator.id,
        )
        session.add(instance)
        session.flush()

        log = WorkflowAuditLog(
            instance_id=instance.id,
            from_state="NONE",
            to_state=initial_state,
            action="INITIALIZE",
            performed_by_id=creator.id,
            comment="Workflow initialized",
        )
        session.add(log)
        return instance

    @classmethod
    def get_allowed_transitions(
        cls,
        instance: WorkflowInstance,
        user: UserInfo,
    ) -> List[WorkflowTransitionConfig]:
        wf_def = BaseWorkflowRegistry.get_workflow(instance.workflow_key)
        if not wf_def:
            return []

        allowed = []
        for t in wf_def.transitions:
            if t.from_state != instance.current_state:
                continue

            if user.is_superuser:
                allowed.append(t)
                continue

            if t.requires_superuser and not user.is_superuser:
                continue

            # Role guard (matches UserRoleManager / User.roles)
            if t.required_roles:
                user_roles = [r.strip().lower() for r in (user.roles or [])]
                if not any(req.strip().lower() in user_roles for req in t.required_roles):
                    continue

            # Department guard
            if t.required_department:
                if (user.department or "").strip().lower() != t.required_department.strip().lower():
                    continue

            # District boundary guard
            if t.enforce_district_scope and user.dist_id is not None:
                if instance.dist_id and instance.dist_id != user.dist_id:
                    continue

            # Subdivision boundary guard
            if t.enforce_subdivision_scope and user.subdiv_id is not None:
                if instance.subdiv_id and instance.subdiv_id != user.subdiv_id:
                    continue

            allowed.append(t)
        return allowed

    @classmethod
    def execute_transition(
        cls,
        session: Session,
        instance_id: int,
        req: ExecuteTransitionRequest,
        user: UserInfo,
        background_tasks: Optional[BackgroundTasks] = None,
    ) -> WorkflowInstance:
        instance = session.query(WorkflowInstance).filter_by(id=instance_id).first()
        if not instance:
            raise HTTPException(status_code=404, detail="Workflow instance not found.")

        allowed = cls.get_allowed_transitions(instance, user)
        transition = next((t for t in allowed if t.action == req.action), None)

        if not transition:
            raise HTTPException(
                status_code=403,
                detail=f"Action '{req.action}' not permitted from '{instance.current_state}' for user '{user.username}'."
            )

        if transition.requires_comment and not req.comment:
            raise HTTPException(status_code=400, detail="A comment is required for this action.")

        # 2FA Verification via GoogleAuthenticatorManager
        if transition.requires_2fa:
            if not req.otp or not req.otp_application_code or not req.otp_team_code:
                raise HTTPException(status_code=400, detail="2FA credentials (OTP, application_code, team_code) required.")
            cls._verify_transition_otp(
                session=session,
                app_code=req.otp_application_code,
                team_code=req.otp_team_code,
                otp=req.otp,
                required_permission=transition.two_factor_permission,
            )

        # Apply state mutation
        old_state = instance.current_state
        instance.current_state = transition.to_state

        log = WorkflowAuditLog(
            instance_id=instance.id,
            from_state=old_state,
            to_state=transition.to_state,
            action=req.action,
            performed_by_id=user.id,
            comment=req.comment,
            extra_payload=req.extra_payload,
        )
        session.add(log)

        # Trigger notification email
        if transition.notification_email_template and background_tasks and user.email:
            subject = f"Workflow Update: {instance.workflow_key} transitioned to {transition.to_state}"
            body = (
                f"Hello {user.name or user.username},\n\n"
                f"Action '{req.action}' was executed on {instance.entity_type} (ID: {instance.entity_id}).\n"
                f"State changed from {old_state} to {transition.to_state}.\n"
                f"Comment: {req.comment or 'None'}\n\n"
                f"Regards,\nDAMapQL Engine"
            )
            background_tasks.add_task(
                EmailUtils.send_email_background,
                to_email=user.email,
                subject=subject,
                body=body,
            )

        return instance

    @classmethod
    def _verify_transition_otp(
        cls,
        session: Session,
        app_code: str,
        team_code: str,
        otp: str,
        required_permission: Optional[str] = None,
    ) -> None:
        normalized_app, normalized_team = _normalize_otp_identity(app_code, team_code)
        try:
            team = _get_authenticator(session, normalized_app, normalized_team, lock=True)
        except ValueError:
            raise HTTPException(status_code=403, detail="Authenticator unavailable.")

        if not team.is_active or team.suspended_at is not None:
            raise HTTPException(status_code=403, detail="Team authenticator inactive or suspended.")

        if required_permission:
            if required_permission.strip().lower() not in [p.lower() for p in (team.permissions or [])]:
                raise HTTPException(status_code=403, detail="Team lacks required 2FA permission.")

        manager = _get_otp_manager()
        secret = manager.decrypt_secret(team.encrypted_totp_secret)
        step = manager.get_matched_step(
            secret=secret,
            otp_code=otp,
            valid_window=1,
            last_accepted_step=team.last_accepted_step,
        )
        if step is None:
            raise HTTPException(status_code=403, detail="Invalid, expired or reused OTP.")

        team.last_accepted_step = step
        session.flush()