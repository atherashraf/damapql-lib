from typing import Any, Set, Optional

from pydantic import BaseModel, ConfigDict, field_serializer
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.damapql.dch.schemas.geojson import Feature, GeometryData


"""
BaseSchema

Purpose:
--------
Reusable base Pydantic schema for all GIS models.

Responsibilities:
-----------------
1. Convert database geometry (PostGIS / GeoAlchemy / Shapely) → GeoJSON geometry
2. Automatically serialize geometry fields (`geom`, `geometry`)
3. Provide helper to convert a row into a GeoJSON Feature

Usage Example:
--------------
from app.damapql.dch.schemas.geojson import GeoJSON

rows = [UcBoundarySchema.model_validate(obj) for obj in objects]

feature_collection = GeoJSON(
    type="FeatureCollection",
    crs="EPSG:3857",
    features=[row.to_feature() for row in rows],
)
"""


class GISSchemaBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @staticmethod
    def geom_to_geojson(geom) -> GeometryData | None:
        if geom is None:
            return None
        return GeometryData(**mapping(to_shape(geom)))

    @field_serializer("geom", check_fields=False)
    def serialize_geom(self, geom):
        return GISSchemaBase.geom_to_geojson(geom)