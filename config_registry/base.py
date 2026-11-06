# app/damapql/config_registry/base.py
from __future__ import annotations
from typing import Any, ClassVar, TypeVar, Generic
from pydantic import BaseModel, Field

T = TypeVar("T")

class BaseConfigRegistry(Generic[T]):
    """
    Generic thread-safe base registry for pluggable domain configurations.
    Applications (WASA, LBDC, GeoCAD) register their concrete implementations
    during application bootstrap.
    """
    _implementation: ClassVar[Any | None] = None

    @classmethod
    def register_implementation(cls, impl: type[T]) -> None:
        if not issubclass(impl, cls):
            raise TypeError(f"{impl.__name__} must inherit from {cls.__name__}")
        cls._implementation = impl

    @classmethod
    def get_implementation(cls) -> type[T]:
        return cls._implementation or cls.get_default_implementation()

    @classmethod
    def get_default_implementation(cls) -> type[T]:
        raise NotImplementedError("Default implementation must be specified")