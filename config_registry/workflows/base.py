# app/damapql/config_registry/workflows/base.py
from __future__ import annotations
from typing import ClassVar, Dict
from app.damapql.config_registry.base import BaseConfigRegistry
from app.damapql.config_registry.workflows.schemas import WorkflowDefinition


class BaseWorkflowRegistry(BaseConfigRegistry["BaseWorkflowRegistry"]):
    """
    Contract for registering application-specific workflows (WASA, LBDC, GeoCAD, Survey).
    Host apps implement `get_workflow_definitions` and register during bootstrap.
    """
    _implementation: ClassVar[type[BaseWorkflowRegistry] | None] = None

    @classmethod
    def get_default_implementation(cls) -> type[BaseWorkflowRegistry]:
        return NullWorkflowRegistry

    @classmethod
    def get_workflow_definitions(cls) -> list[WorkflowDefinition]:
        return []

    @classmethod
    def get_workflow(cls, workflow_key: str) -> WorkflowDefinition | None:
        impl = cls.get_implementation()
        for wf in impl.get_workflow_definitions():
            if wf.workflow_key == workflow_key:
                return wf
        return None


class NullWorkflowRegistry(BaseWorkflowRegistry):
    @classmethod
    def get_workflow_definitions(cls) -> list[WorkflowDefinition]:
        return []