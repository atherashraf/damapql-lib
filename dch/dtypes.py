import os
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Union, Literal

from digitalarzengine.io.managers.db_manager import DBString
from pydantic import BaseModel, RootModel, ConfigDict, field_validator, Field


class DBConnectionCreate(BaseModel):
    connection_name: str  # instead of name
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
                "port": "****"
            }
        }
    )


class DBConnectionType(BaseModel):
    id: int
    name: str
    params: Optional[DBCredential]

    model_config = ConfigDict(from_attributes=True)


class LayerCategoryType(BaseModel):
    id: int
    main: str
    sub: Optional[str]
    model_config = ConfigDict(from_attributes=True)


class LayerColumnParam(BaseModel):
    name: str
    d_type: Optional[str] = None


class DBAccessibilityParams(BaseModel):
    conn_name: str
    table_name: str
    columns: List[LayerColumnParam] = []
    g_col: Optional[str] = None
    pk_cols: List[str] = []


class URLAccessibilityParams(BaseModel):
    url_type: Optional[str] = None
    url: Optional[str] = None


import os
from typing import Literal, Optional
from pydantic import BaseModel, model_validator

# 🌟 Added "ftp" to PathBaseType
PathBaseType = Literal["media_rel", "s3", "absolute", "url", "ftp"]


class FileAccessibilityParams(BaseModel):
    file_path: str
    path_base: PathBaseType = "media_rel"

    # Kept for backward compatibility when reading existing DB JSONB params
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

        # "url", "s3", "ftp", and "absolute" return the raw protocol string
        if self.path_base in ("url", "s3", "ftp", "absolute"):
            return fp

        return fp


class LayerAccessibilityType(BaseModel):
    id: int
    accessibility_type: Optional[str]
    params: Union[
        DBAccessibilityParams,
        URLAccessibilityParams,
        FileAccessibilityParams,
        Dict[str, Any],
    ]

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

        if acc_type == "DB":
            return DBAccessibilityParams(**v)

        if acc_type == "URL":
            return URLAccessibilityParams(**v)

        # file-like payloads
        if "file_path" in v:
            return FileAccessibilityParams(**v)

        return v


# class LayerAccessibilityType(BaseModel):
#     id: int
#     accessibility_type: Optional[str]
#     params: Dict[str, Any]
#     model_config = ConfigDict(from_attributes=True)


class LayerQualityLevelType(BaseModel):
    id: int
    level: int
    name: str
    description: Optional[str]
    model_config = ConfigDict(from_attributes=True)


class LayerInfoType(BaseModel):
    id: int
    uuid: str
    app_label: str
    title: str
    layer_name: str
    srid: Optional[int]
    proj4: Optional[str]
    extent: Optional[List[float]]
    extent_3857: Optional[List[float]]
    data_model: Any  # Assuming ChoiceType was backed by str
    geom_type: Optional[List[str]]
    layer_styling: Optional[Dict[str, Any]]
    zoom_range: List[int]
    uploaded_by: Optional[int]
    uploaded_date: datetime
    temporal_res: Optional[date]
    extra_metadata: Optional[Dict[str, Any]]
    category_id: Optional[int]
    layer_access_id: int
    quality_level_id: Optional[int]
    data_url: Optional[str]
    layer_setting: Optional[Dict[str, Any]]
    format: str
    date_range_url: Optional[str]
    model_type: Optional[str]

    # Relationships (optional or nested models if you want)
    layer_category: Optional[Union[LayerCategoryType, int]]
    # layer_accessibility: Optional[Union[LayerAccessibilityType,int]]
    layer_accessibility: Union[int, LayerAccessibilityType]
    quality_level: Optional[Union[LayerQualityLevelType, int]]

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm(cls, obj):
        res = cls.model_validate({
            **obj.__dict__,
            "data_model": obj.data_model.value,
            "model_type": obj.model_type.value if obj.model_type else None
        })
        return res

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

    # @field_validator("layer_styling", "layer_setting", "extra_metadata", mode="before")
    # @classmethod
    # def empty_str_to_none_for_dict_fields(cls, v):
    #     if v == "":
    #         return None
    #     return v


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
    def normalize_zoom_range(
            cls,
            value: Any,
    ) -> List[int]:
        if value is None:
            return [0, 30]

        return value

    model_config = ConfigDict(extra='allow', from_attributes=True)


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
    layers: List[LayerItem]
    srid: int = 3857
    units: str = "m"
    description: Optional[str] = None
    navigation_setting: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class GridPayload(BaseModel):
    pkCols: List[str] = Field(default_factory=list)
    columns: List[Dict[str, Any]] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)


class LayerInfoListResponse(BaseModel):
    payload: GridPayload


class MapInfoResponse(BaseModel):
    payload: MapInfoType  # More detailed modeling is possible later


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
    std: Optional[float]
    median: Optional[float]
    count: Optional[int]
    area_sq_km: Optional[float]  # only in class-based raster summaries


# class RasterAttributesResponse(BaseModel):
#     payload: Dict[str, Union[int, Dict[str, RasterBandSummary]]]


class UploadRasterResponse(BaseModel):
    uuid: str
    msg: str
    path: str
