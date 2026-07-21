from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class GeomStyle(BaseModel):
    pointShape: Optional[str] = None
    pointSize: Optional[float] = None
    strokeColor: Optional[str] = None
    strokeWidth: Optional[float] = None
    fillColor: Optional[str] = None
    pointIconSrc: Optional[str] = None

class Filter(BaseModel):
    field: str
    op: Literal["==", ">=", "<=", ">", "<", "!=", "between"]
    value: str | List[float]
    logicalOp: Optional[Literal["And", "Or"]] = None

class Rule(BaseModel):
    title: str
    filter: Optional[Filter] = None
    style: GeomStyle

class FeatureStyle(BaseModel):
    type: Literal["single", "multiple", "density", "sld"]
    # keep flexible to match your TS structure: {"default"?: GeomStyle, "rules"?: Rule[]}
    style: Dict[str, Any]

# class DALayer(BaseModel):
#     uuid: str
#     opacity: float = Field(ge=0.0, le=1.0)
#     visible: bool
#     # allow client-side override
#     style: Optional[FeatureStyle] = None
#
# class OtherLayer(BaseModel):
#     name: str
#     type: str
#     params: Dict[str, Any] = Field(default_factory=dict)

class LayerGroupSaveSchema(BaseModel):
    key: str
    title: str
    order: int = 0
    collapsed: bool = False


class LayerSaveSchema(BaseModel):
    uuid: str
    visible: bool
    opacity: float = Field(ge=0.0, le=1.0)
    zIndex: int
    title: Optional[str] = None
    name: Optional[str] = None
    style: Optional[Any] = None
    groupKey: Optional[str] = None
    groupTitle: Optional[str] = None


class OtherLayerSaveSchema(BaseModel):
    uuid: str = "-1"
    name: str
    title: Optional[str] = None
    type: str
    visible: bool = True
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    zIndex: int = 0
    groupKey: Optional[str] = None
    groupTitle: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class MapSaveRequest(BaseModel):
    uuid: str
    mapName: str
    extent: List[float]
    baseLayer: str = "Google Hybrid"
    groups: List[LayerGroupSaveSchema] = Field(default_factory=list)
    daLayers: List[LayerSaveSchema] = Field(default_factory=list)
    otherLayers: List[OtherLayerSaveSchema] = Field(default_factory=list)