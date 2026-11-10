# app/damapql/config_registry/__init__.py
from fastapi import APIRouter

# Boundaries
from app.damapql.config_registry.boundaries.base import BaseBoundaryHierarchy
from app.damapql.config_registry.boundaries.schemas import (
    BoundaryLevelConfig,
    BoundaryModeConfig,
    BoundaryOption,
)
from app.damapql.config_registry.boundaries.service import BoundaryHierarchyService
from app.damapql.config_registry.boundaries.router import boundary_config_router

# AOI
from app.damapql.config_registry.aoi.base import BaseUserAOI, NullUserAOI
from app.damapql.config_registry.aoi.service import UserAOIService

# Workflows
from app.damapql.config_registry.workflows.base import BaseWorkflowRegistry, NullWorkflowRegistry
from app.damapql.config_registry.workflows.service import WorkflowEngineService
from app.damapql.config_registry.workflows.router import workflow_config_router
from app.damapql.config_registry.workflows.schemas import (
    WorkflowDefinition,
    WorkflowStateConfig,
    WorkflowTransitionConfig,
)

# Unified Config Router
config_registry_router = APIRouter(prefix="/config")
config_registry_router.include_router(boundary_config_router)
config_registry_router.include_router(workflow_config_router)

__all__ = [
    # Boundaries
    "BaseBoundaryHierarchy",
    "BoundaryLevelConfig",
    "BoundaryModeConfig",
    "BoundaryOption",
    "BoundaryHierarchyService",
    "boundary_config_router",
    # AOI
    "BaseUserAOI",
    "NullUserAOI",
    "UserAOIService",
    # Workflows
    "BaseWorkflowRegistry",
    "NullWorkflowRegistry",
    "WorkflowEngineService",
    "WorkflowDefinition",
    "WorkflowStateConfig",
    "WorkflowTransitionConfig",
    "workflow_config_router",
    # Root
    "config_registry_router",
]