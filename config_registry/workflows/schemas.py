# app/damapql/config_registry/workflows/schemas.py
from __future__ import annotations
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field


class WorkflowTransitionConfig(BaseModel):
    action: str = Field(..., description="Action key e.g., 'SUBMIT', 'REJECT', 'APPROVE'")
    label: str = Field(..., description="Human-readable button label e.g., 'Submit for Verification'")
    from_state: str = Field(..., description="Source state key")
    to_state: str = Field(..., description="Target state key")

    # Auth & RBAC Guards
    required_roles: List[str] = Field(default_factory=list, description="Roles permitted to execute this transition")
    required_department: Optional[str] = Field(default=None, description="Department name required for this step")
    requires_superuser: bool = False

    # 2FA Gate
    requires_2fa: bool = False
    two_factor_permission: Optional[str] = Field(default=None, description="Permission string verified via TOTP")

    # Boundary Guard
    enforce_district_scope: bool = False
    enforce_subdivision_scope: bool = False

    # Verification & Email Notifications
    requires_comment: bool = False
    notification_email_template: Optional[str] = None  # e.g., 'workflow_state_updated'


class WorkflowStateConfig(BaseModel):
    key: str = Field(..., description="Unique state key e.g., 'DRAFT', 'GIS_VERIFICATION'")
    label: str = Field(..., description="Display title e.g., 'GIS Verification'")
    is_initial: bool = False
    is_terminal: bool = False
    badge_color: str = "#757575"


class WorkflowDefinition(BaseModel):
    workflow_key: str = Field(..., description="Unique workflow identifier e.g., 'society_noc', 'survey_task'")
    title: str = Field(..., description="Workflow title")
    states: List[WorkflowStateConfig]
    transitions: List[WorkflowTransitionConfig]


class ExecuteTransitionRequest(BaseModel):
    action: str
    comment: Optional[str] = None
    extra_payload: Dict[str, Any] = Field(default_factory=dict)
    otp: Optional[str] = Field(default=None, description="6-digit OTP if transition is 2FA gated")
    otp_application_code: Optional[str] = None
    otp_team_code: Optional[str] = None


class WorkflowAuditEntry(BaseModel):
    id: int
    from_state: str
    to_state: str
    action: str
    performed_by_id: int
    performed_by_username: str
    comment: Optional[str] = None
    timestamp: str