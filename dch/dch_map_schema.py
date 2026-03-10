# dch_map_schema.py (Pydantic v2)
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

class DALayer(BaseModel):
    uuid: str
    opacity: float = Field(ge=0.0, le=1.0)
    visible: bool
    # allow client-side override
    style: Optional[FeatureStyle] = None

class OtherLayer(BaseModel):
    name: str
    type: str
    params: Dict[str, Any] = Field(default_factory=dict)

class MapSaveRequest(BaseModel):
    baseLayer: str
    daLayers: List[DALayer] = Field(default_factory=list)
    # Pydantic v2: use Field(min_length=, max_length=) instead of conlist
    extent: List[float] = Field(min_length=4, max_length=4)
    mapName: str
    uuid: str = "-1"
    # default to [] so you never hit KeyError
    otherLayers: List[OtherLayer] = Field(default_factory=list)
