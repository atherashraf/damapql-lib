from enum import Enum

class APIEnum(str, Enum):
    @classmethod
    def choices(cls):
        return [(e.name, e.value) for e in cls]

    @classmethod
    def get_name(cls, value):
        for e in cls:
            if e.value == value:
                return e.name
        raise ValueError(f"{value} is not a valid value for {cls.__name__}")


class AccessibilityType(APIEnum):
    File = 'File'
    DB = 'DB'
    S3 = 'S3'
    Minio = 'Minio'
    D_DB = 'Django Database Key'
    URL = 'url'
    Application = 'Application'


class DataModel(APIEnum):
    R = "Raster"
    V = "Vector"
    T = "Tabular"
    D = "Document"
    URL = "URL"
    Application = "Application"


class ModelType(APIEnum):
    # Vector models
    Tin = "TIN"
    Network = "Network"
    Geom = "Geometry"
    Contour = "Contour"

    # Raster models
    Satellite = "Satellite"
    DEM = "Digital Elevation Model"
    DSM = "Digital Surface Model"
    Surface = "Surface"
    Classification = "Classification"

    # Tabular models
    Table = "Table"
    Telemetry = "Telemetry"

    # Document models
    PDF = "PDF"
    DOC = "Word Document"
    Txt = "Text Document"
    Latex = "LaTeX Document"

    # General / shared
    Application = "Application"
    Api = "Api"
    Other = "Other"

    @classmethod
    def vector_types(cls) -> list["ModelType"]:
        """Returns ModelTypes applicable to Vector datasets."""
        return [
            cls.Geom,
            cls.Network,
            cls.Contour,
            cls.Tin,
            cls.Other,
        ]

    @classmethod
    def raster_types(cls) -> list["ModelType"]:
        """Returns ModelTypes applicable to Raster datasets."""
        return [
            cls.Satellite,
            cls.DEM,
            cls.DSM,
            cls.Surface,
            cls.Classification,
            cls.Other,
        ]

    @classmethod
    def tabular_types(cls) -> list["ModelType"]:
        """Returns ModelTypes applicable to Tabular datasets."""
        return [
            cls.Table,
            cls.Telemetry,
            cls.Other,
        ]

    @classmethod
    def document_types(cls) -> list["ModelType"]:
        """Returns ModelTypes applicable to Document assets."""
        return [
            cls.PDF,
            cls.DOC,
            cls.Txt,
            cls.Latex,
            cls.Other,
        ]

    @classmethod
    def by_data_model(cls, data_model: str | None = None) -> list[dict[str, str]]:
        """Returns [{key, value}] choices filtered by DataModel ('V', 'R', 'T', 'D', or all)."""
        normalized = (data_model or "").strip().upper()
        if normalized in ("V", "VECTOR"):
            items = cls.vector_types()
        elif normalized in ("R", "RASTER"):
            items = cls.raster_types()
        elif normalized in ("T", "TABULAR"):
            items = cls.tabular_types()
        elif normalized in ("D", "DOCUMENT"):
            items = cls.document_types()
        else:
            items = list(cls)

        return [{"key": item.name, "value": item.value} for item in items]


class GeomType(APIEnum):
    Point = "Point"
    LineString = "LineString"
    Polygon = "Polygon"
    MultiPoint = "MultiPoint"
    MultiLineString = "MultiLineString"
    MultiPolygon = "MultiPolygon"


class ApplicationURLType(str, Enum):
    geoserver = "geoserver"
    arcgis = "arcgis"