# app/damapql/config_registry/boundaries/schemas.py
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field


class BoundaryLevelConfig(BaseModel):
    level: int = Field(..., description="Hierarchy depth level (1, 2, 3...)")
    name: str = Field(..., description="Level identifier e.g., 'wasa_division'")
    label: str = Field(..., description="Display title e.g., 'WASA Division'")
    table_name: str = Field(..., description="Database boundary table name")
    schema_name: str = Field(default="public", description="Database schema name")
    pk_col: str = Field(default="id", description="Primary key column")
    label_col: str = Field(default="name", description="Column for dropdown label")
    parent_fk_col: Optional[str] = Field(default=None, description="Foreign key linking to parent PK")
    where_clause: Optional[str] = Field(default=None, description="Static SQL filter condition")
    db_key: Optional[str] = Field(default=None, description="Database connection key override")


class BoundaryModeConfig(BaseModel):
    mode_key: str = Field(..., description="Mode identifier e.g. 'wasa', 'province'")
    label: str = Field(..., description="Mode display label")
    db_key: Optional[str] = Field(default=None, description="Default DB key for this mode")
    levels: List[BoundaryLevelConfig]


class BoundaryOption(BaseModel):
    key: str | int
    label: str
