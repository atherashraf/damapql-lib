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

class LayerSaveSchema(BaseModel):
    uuid: str
    visible: bool
    opacity: float
    zIndex: int  # Added to capture the drag order
    title: Optional[str] = None
    style: Optional[Any] = None

class MapSaveRequest(BaseModel):
    uuid: str
    mapName: str
    extent: List[float]
    baseLayer: str = "Google Hybrid"
    daLayers: List[LayerSaveSchema]
    otherLayers: List[Any] = []

