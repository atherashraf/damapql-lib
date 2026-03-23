from typing import Any

from pydantic import BaseModel, Field


class LayerAttributeQueryRequest(BaseModel):
    cols: list[str] | None = None
    where_clause: str | None = None
    limit: int = Field(default=100, ge=1, le=5000)
    offset: int = Field(default=0, ge=0)


class FeatureByPkRequest(BaseModel):
    pk_values: list[str] = Field(default_factory=list)


class ColumnValueRequest(BaseModel):
    pk_values: list[str] = Field(default_factory=list)
    col_name: str


class UpdateColumnValueRequest(BaseModel):
    pk_values: list[str] = Field(default_factory=list)
    col_name: str
    value: Any