from enum import Enum

class APIEnum(str, Enum):
    @classmethod
    def choices(cls):
        return [(e.name, e.value) for e in cls]


class AccessibilityType(APIEnum):
    File = 'File'
    DB = 'Database'
    D_DB = 'Django Database Key'
    URL = 'url'


class DataModel(APIEnum):
    R = 'Raster'
    V = 'Vector'
    URL= 'URL'


class GeomType(APIEnum):
    Point = "Point"
    LineString = "LineString"
    Polygon = "Polygon"
    MultiPoint = "MultiPoint"
    MultiLineString = "MultiLineString"
    MultiPolygon = "MultiPolygon"



class ModelType(APIEnum):
    # vector model
    Tin = 'TIN'
    Network = 'Network'
    Geom = 'Geometry'
    Contour = 'Contour'
    # raster model
    Satellite = 'Satellite'
    DEM = 'Digital Elevation Model'
    DSM = 'Digital Surface Model'
    Surface = 'Surface'
    Classification = 'Classification'
    Other = 'Other'
