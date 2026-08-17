from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Union, Annotated, Tuple

from app.damapql.dch.api_enum import AccessibilityType
from digitalarzengine.io.managers.db_manager import DBString
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# -----------------------------------------------------------------------------
# Database Models
# -----------------------------------------------------------------------------

class DBConnectionCreate(BaseModel):
    connection_name: str
    engine: str
    params: DBString


class DBCredential(BaseModel):
    engine: str
    host: str
    user: str
    password: str
    name: str
    port: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "engine": "postgresql",
                "host": "***.***.**.***",
                "user": "********",
                "password": "*********",
                "name": "*******",
                "port": "5432",
            }
        }
    )


class DBConnectionType(BaseModel):
    id: int
    name: str
    params: Optional[DBCredential] = None

    model_config = ConfigDict(from_attributes=True)


class LayerCategoryType(BaseModel):
    id: int
    main: str
    sub: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class LayerQualityLevelType(BaseModel):
    id: int
    level: int
    name: str
    description: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# -----------------------------------------------------------------------------
# Accessibility Parameter Schemas
# -----------------------------------------------------------------------------



class AccessibilityParams(BaseModel):
    type: AccessibilityType
    model_config = ConfigDict(
        from_attributes=True,
        extra="allow",
    )



class LayerColumnParam(BaseModel):
    name: str
    d_type: Optional[str] = None

class DBAccessibilityParams(AccessibilityParams):
    type: Literal["db"] = "db"

    conn_name: str
    table_name: str

    columns: List[LayerColumnParam] = Field(default_factory=list)

    g_col: Optional[str] = None
    pk_cols: List[str] = Field(default_factory=list)



class URLAccessibilityParams(AccessibilityParams):
    type: Literal["url"] = "url"

    url_type: Optional[str] = None
    url: str


class MinioAccessibilityParams(AccessibilityParams):
    type: Literal["minio"] = "minio"

    bucket: str
    object_name: str

    spectral_resolution: Optional[int] = None
    spatial_resolution: Optional[
        Union[float, Tuple[float, float]]
    ] = None
    radiometric_resolution: Optional[str] = None

    @property
    def s3_uri(self) -> str:
        return f"s3://{self.bucket}/{self.object_name.lstrip('/')}"

class S3AccessibilityParams(AccessibilityParams):
    type: Literal["s3"] = "s3"

    bucket: str
    object_name: str

    spectral_resolution: Optional[int] = None
    spatial_resolution: Optional[Union[float, List[float]]] = None
    radiometric_resolution: Optional[str] = None

    @property
    def s3_uri(self) -> str:
        return f"s3://{self.bucket}/{self.object_name.lstrip('/')}"




PathBaseType = Literal["media_rel", "s3", "absolute", "url", "ftp"]


class FileAccessibilityParams(AccessibilityParams):
    type: Literal["file"] = "file"
    file_path: str
    path_base: PathBaseType = "media_rel"
    is_path_media_rel: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_and_infer_path_base(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            return data

        # If path_base is explicitly provided, respect it
        if "path_base" in data and data["path_base"]:
            return data

        fp = str(data.get("file_path", "")).strip().lower()

        # 1. Backwards-compatibility check for legacy 'is_path_media_rel' flag
        legacy_media_flag = data.get("is_path_media_rel")
        if legacy_media_flag is not None:
            if isinstance(legacy_media_flag, str):
                legacy_media_flag = legacy_media_flag.strip().lower() in {"true", "1", "yes", "y"}

            if legacy_media_flag:
                data["path_base"] = "media_rel"
                return data

        # 2. Infer path_base from file_path structure if omitted
        if fp.startswith("s3://"):
            data["path_base"] = "s3"
        elif fp.startswith(("ftp://", "ftps://")):
            data["path_base"] = "ftp"
        elif fp.startswith(("http://", "https://")):
            data["path_base"] = "url"
        elif os.path.isabs(data.get("file_path", "")):
            data["path_base"] = "absolute"
        else:
            data["path_base"] = "media_rel"

        return data

    @property
    def clean_relative_path(self) -> str:
        """Strips protocol prefixes (like s3://bucket/) if present."""
        fp = self.file_path.strip()
        if fp.startswith("s3://"):
            parts = fp.replace("s3://", "").split("/", 1)
            return parts[1] if len(parts) > 1 else parts[0]
        return fp.lstrip("/\\")

    def resolve_file_path(self, media_dir: str) -> str:
        """Deterministically resolves the physical path based on path_base."""
        fp = self.file_path.strip()
        if not fp:
            return fp
        if self.path_base == "media_rel":
            return os.path.join(media_dir, self.clean_relative_path)
        return fp


LayerAccessibilityParams = Annotated[
    Union[
        DBAccessibilityParams,
        URLAccessibilityParams,
        MinioAccessibilityParams,
        S3AccessibilityParams,
        FileAccessibilityParams,
    ],
    Field(discriminator="type"),
]


class LayerAccessibilityType(BaseModel):
    id: int
    accessibility_type: Optional[str] = None
    params: LayerAccessibilityParams

    model_config = ConfigDict(from_attributes=True)

    @field_validator("accessibility_type", mode="before")
    @classmethod
    def _coerce_choice(cls, v):
        if v is None:
            return None
        code = getattr(v, "code", None)
        if code is not None:
            return code
        name = getattr(v, "name", None)
        if name is not None:
            return name
        return str(v)

    @field_validator("params", mode="before")
    @classmethod
    def _coerce_params(cls, v, info):
        if not isinstance(v, dict):
            return v

        data = info.data or {}
        acc_type = data.get("accessibility_type")

        if hasattr(acc_type, "code"):
            acc_type = acc_type.code
        elif hasattr(acc_type, "name"):
            acc_type = acc_type.name
        elif acc_type is not None:
            acc_type = str(acc_type)

        if acc_type in ("S3", "MinIO") or ("bucket" in v and "object_name" in v):
            return S3AccessibilityParams(**v)

        if acc_type == "DB":
            return DBAccessibilityParams(**v)

        if acc_type == "URL":
            return URLAccessibilityParams(**v)

        if "file_path" in v:
            return FileAccessibilityParams(**v)

        return v


# -----------------------------------------------------------------------------
# Layer & Map Schemas
# -----------------------------------------------------------------------------

class LayerInfoType(BaseModel):
    id: int
    uuid: str
    app_label: str
    title: str
    layer_name: str
    srid: Optional[int] = None
    proj4: Optional[str] = None
    extent: Optional[List[float]] = None
    extent_3857: Optional[List[float]] = None
    data_model: Any
    geom_type: Optional[List[str]] = None
    layer_styling: Optional[Dict[str, Any]] = None
    zoom_range: List[int] = Field(default_factory=lambda: [0, 30])
    uploaded_by: Optional[int] = None
    uploaded_date: datetime
    temporal_res: Optional[date] = None
    extra_metadata: Optional[Dict[str, Any]] = None
    category_id: Optional[int] = None
    layer_access_id: int
    quality_level_id: Optional[int] = None
    data_url: Optional[str] = None
    layer_setting: Optional[Dict[str, Any]] = None
    format: str = "COG"
    date_range_url: Optional[str] = None
    model_type: Optional[str] = None

    layer_category: Optional[Union[LayerCategoryType, int]] = None
    layer_accessibility: Union[int, LayerAccessibilityType]
    quality_level: Optional[Union[LayerQualityLevelType, int]] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("layer_accessibility", mode="before")
    @classmethod
    def _coerce_relationship(cls, v):
        # If ORM object, map to a dict expected by LayerAccessibilityType
        try:
            if hasattr(v, "id"):
                acc_type = getattr(v, "accessibility_type", None)
                code = getattr(acc_type, "code", None) if acc_type is not None else None
                if code is None and acc_type is not None:
                    code = getattr(acc_type, "name", None) or str(acc_type)
                return {
                    "id": v.id,
                    "accessibility_type": code,
                    "params": getattr(v, "params", {}) or {},
                }
        except Exception:
            pass
        return v


class LayerItem(BaseModel):
    uuid: str
    layerName: Optional[str] = None
    title: Optional[str] = None
    visible: bool = True
    opacity: float = 1.0
    zIndex: int = 0
    style: Optional[Any] = None
    zoomRange: List[int] = Field(default_factory=lambda: [0, 30])
    isBase: bool = False
    key: Optional[str] = None
    groupKey: Optional[str] = None
    groupTitle: Optional[str] = None
    isGroupMeta: bool = False
    groups: Optional[List[Dict[str, Any]]] = None

    @field_validator("zoomRange", mode="before")
    @classmethod
    def normalize_zoom_range(cls, value: Any) -> List[int]:
        return [0, 30] if value is None else value

    model_config = ConfigDict(extra="allow", from_attributes=True)


class LayerGroupItem(BaseModel):
    key: str
    title: str
    order: int = 0
    collapsed: bool = False


class MapInfoType(BaseModel):
    id: Optional[int] = None
    uuid: str
    title: str
    extent: List[float]
    groups: List[LayerGroupItem] = Field(default_factory=list)
    layers: List[LayerItem] = Field(default_factory=list)
    srid: int = 3857
    units: str = "m"
    description: Optional[str] = None
    navigation_setting: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


# -----------------------------------------------------------------------------
# Response Data Transfer Objects
# -----------------------------------------------------------------------------

class GridPayload(BaseModel):
    pkCols: List[str] = Field(default_factory=list)
    columns: List[Dict[str, Any]] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)


class LayerInfoListResponse(BaseModel):
    payload: GridPayload


class MapInfoResponse(BaseModel):
    payload: MapInfoType


class TableColumn(BaseModel):
    id: str
    label: str
    type: Literal["string", "number", "date"]
    disablePadding: bool = False


class AttributeTablePayload(GridPayload):
    object_name: Optional[str] = None
    object_type: Optional[Literal["model_name", "table_name"]] = None
    layer_uuid: Optional[str] = None


class AttributeTableResponse(BaseModel):
    payload: AttributeTablePayload


class RasterBandSummary(BaseModel):
    min: float
    max: float
    mean: float
    std: Optional[float] = None
    median: Optional[float] = None
    count: Optional[int] = None
    area_sq_km: Optional[float] = None


class UploadRasterResponse(BaseModel):
    uuid: str
    msg: str