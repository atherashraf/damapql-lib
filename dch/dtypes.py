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


class FileAccessibilityParams(BaseModel):
    file_path: str
    is_path_media_rel: bool = False

    @field_validator("is_path_media_rel", mode="before")
    @classmethod
    def parse_bool(cls, v):
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip().lower() in {"true", "1", "yes", "y"}
        return bool(v)

    @property
    def is_s3(self) -> bool:
        return self.file_path.startswith("s3://")

    @property
    def is_absolute_local(self) -> bool:
        if self.is_s3:
            return False
        return os.path.isabs(self.file_path)

    @property
    def is_media_relative(self) -> bool:
        return (not self.is_s3) and (not self.is_absolute_local) and self.is_path_media_rel

    def resolve_file_path(self, media_dir: str) -> str:
        """
        Returns a usable path string.

        Rules:
        - s3://... stays unchanged
        - absolute local path stays unchanged
        - relative path + is_path_media_rel=True => join with media_dir
        - other relative path stays unchanged
        """
        fp = self.file_path
        if not fp:
            return fp

        if self.is_s3:
            return fp

        if os.path.isabs(fp):
            return fp

        if self.is_path_media_rel:
            return os.path.join(media_dir, fp)

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
    model_type: Optional[Any]
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
    model_name: Optional[str]

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

    # make optional / defaulted so base layers validate
    style: Optional[Any] = None
    visible: bool = True
    zoomRange: List[int] = Field(default_factory=lambda: [0, 30])

    # soak up fields used by base/other layers without breaking validation
    isBase: Optional[bool] = None
    key: Optional[str] = None
    opacity: float = 1.0

    # allow & keep unknown extra fields (e.g., params, type)
    model_config = ConfigDict(extra='allow')


class MapInfoType(BaseModel):
    id: int
    uuid: str
    title: str
    extent: List[float]  # [minX, minY, maxX, maxY] in map units
    layers: List[LayerItem]  # JSON → Python dict
    srid: int
    units: str
    description: Optional[str] = None
    navigation_setting: Optional[Dict[str, Any]] = None

    # Tell Pydantic to build from SQLAlchemy objects
    model_config = ConfigDict(from_attributes=True)


class GridPayload(BaseModel):
    pkCols: List[str]
    columns: List[Dict[str, Any]]
    rows: List[Dict[str, Any]]


class LayerInfoListResponse(BaseModel):
    payload: GridPayload


class MapInfoResponse(BaseModel):
    payload: MapInfoType  # More detailed modeling is possible later


class TableColumn(BaseModel):
    id: str
    label: str
    type: Literal["string", "number", "date"]
    disablePadding: bool = False


class AttributeTablePayload(BaseModel):
    pkCols: List[str]
    columns: List[TableColumn]
    rows: List[Dict[str, Any]]


class AttributeTableResponse(BaseModel):
    # kind: Literal["vector"] = "vector"
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
