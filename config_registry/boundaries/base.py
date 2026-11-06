# app/damapql/config_registry/boundaries/base.py
from __future__ import annotations
from typing import ClassVar
from app.damapql.config_registry.base import BaseConfigRegistry
from app.damapql.config_registry.boundaries.schemas import BoundaryModeConfig


class BaseBoundaryHierarchy(BaseConfigRegistry["BaseBoundaryHierarchy"]):
    """
    Contract for application-specific boundary navigation structures.
    """
    _implementation: ClassVar[type[BaseBoundaryHierarchy] | None] = None

    @classmethod
    def get_default_implementation(cls) -> type[BaseBoundaryHierarchy]:
        return NullBoundaryHierarchy

    @classmethod
    def get_modes(cls) -> list[BoundaryModeConfig]:
        """Returns the registered boundary modes for the application."""
        raise NotImplementedError


class NullBoundaryHierarchy(BaseBoundaryHierarchy):
    """Fallback implementation when no app hierarchy is registered."""
    @classmethod
    def get_modes(cls) -> list[BoundaryModeConfig]:
        return []