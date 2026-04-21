from __future__ import annotations

import json
from typing import Any, Literal, Set

from geoalchemy2 import WKBElement
from geoalchemy2.shape import from_shape
from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyproj import CRS
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry


ALLOWED_EPSG = {32642, 32643, 4326, 3857}


# -------------------------------------------------------
# CRS NORMALIZATION UTILITY
# -------------------------------------------------------
def normalize_to_epsg(
    crs_input: Any,
    allowed_epsg: set[int] | None = None,
    strict: bool = True,
) -> str | None:
    """
    Convert any CRS representation into 'EPSG:<code>'.
    """

    if crs_input is None:
        return "EPSG:4326"  # Default GeoJSON assumption

    try:
        crs_obj = CRS.from_user_input(crs_input)
    except Exception as e:
        raise ValueError(f"Invalid CRS input: {crs_input}") from e

    epsg = crs_obj.to_epsg()

    if epsg is None:
        auth = crs_obj.to_authority()
        if auth and auth[0] == "EPSG":
            epsg = int(auth[1])

    if epsg is None:
        if strict:
            raise ValueError(f"CRS cannot be resolved to EPSG: {crs_input}")
        return None

    if allowed_epsg and epsg not in allowed_epsg:
        raise ValueError(f"Unsupported EPSG:{epsg}")

    return f"EPSG:{epsg}"


# -------------------------------------------------------
# Geometry Model
# -------------------------------------------------------
class GeometryData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    coordinates: Any

    def to_shapely(self) -> BaseGeometry:
        return shape(self.model_dump())

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_shapely(cls, geom: BaseGeometry) -> "GeometryData":
        return cls(**mapping(geom))

    def to_db_geometry(self, srid: int = 4326) -> WKBElement:
        return from_shape(self.to_shapely(), srid=srid)


# -------------------------------------------------------
# Feature Model
# -------------------------------------------------------
class Feature(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["Feature"] = "Feature"
    geometry: GeometryData
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry", mode="before")
    @classmethod
    def _coerce_geometry(cls, v):
        if isinstance(v, GeometryData):
            return v
        if isinstance(v, dict):
            return GeometryData(**v)
        raise TypeError("Invalid geometry")

    def to_shapely(self) -> BaseGeometry:
        return self.geometry.to_shapely()

    def to_feature_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "geometry": self.geometry.to_dict(),
            "properties": self.properties or {},
        }

    def to_gdf(self, crs: Any | None = None) -> GeoDataFrame:
        crs_norm = normalize_to_epsg(crs, ALLOWED_EPSG) if crs else None
        return GeoDataFrame(
            [self.properties or {}],
            geometry=[self.to_shapely()],
            crs=crs_norm,
        )


    @classmethod
    def from_shapely(
        cls,
        geom: BaseGeometry,
        properties: dict[str, Any] | None = None,
    ) -> "Feature":
        return cls(
            geometry=GeometryData.from_shapely(geom),
            properties=properties or {},
        )

    def to_db_geometry(self, srid: int = 4326):
        return from_shape(self.to_shapely(), srid=srid)


# -------------------------------------------------------
# GeoJSON Model
# -------------------------------------------------------
class GeoJSON(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: Literal["FeatureCollection"] = "FeatureCollection"

    features: list[Feature] = Field(default_factory=list)

    crs: str | None = Field(
        None,
        description="CRS stored as 'EPSG:<code>'",
    )

    style: dict[str, Any] | None = None

    # -------------------------------------------------------
    # CRS NORMALIZATION AT CREATION
    # -------------------------------------------------------
    @field_validator("crs", mode="before")
    @classmethod
    def _normalize_crs(cls, v):
        return normalize_to_epsg(v, ALLOWED_EPSG)

    # -------------------------------------------------------
    # INTERNAL CRS RESOLUTION
    # -------------------------------------------------------
    def _get_source_epsg(self, assume_epsg: int | None = None) -> int:
        if self.crs:
            return int(self.crs.split(":")[1])

        if assume_epsg is not None:
            if assume_epsg not in ALLOWED_EPSG:
                raise ValueError(f"Unsupported assumed CRS: {assume_epsg}")
            return assume_epsg

        raise ValueError("CRS unknown. Provide crs or assume_epsg.")

    # -------------------------------------------------------
    # CONVERSIONS
    # -------------------------------------------------------
    def to_gdf(self, assume_epsg: int | None = None) -> GeoDataFrame:
        geometries = []
        properties = []

        for f in self.features:
            try:
                geom = f.to_shapely()
                if geom.is_empty:
                    continue
                geometries.append(geom)
                properties.append(f.properties or {})
            except Exception:
                continue

        gdf = GeoDataFrame(properties, geometry=geometries)

        epsg = self._get_source_epsg(assume_epsg)

        if gdf.crs is None:
            gdf.set_crs(epsg=epsg, inplace=True)
        else:
            if gdf.crs.to_epsg() != epsg:
                gdf = gdf.to_crs(epsg=epsg)

        return gdf

    def to_feature_array(self):
        return [f.to_feature_dict() for f in self.features]

    def to_db_geometries(self, srid=4326):
        return [f.to_db_geometry(srid) for f in self.features]

    def to_crs(self, target_epsg: int, assume_epsg: int | None = None) -> "GeoJSON":
        if target_epsg not in ALLOWED_EPSG:
            raise ValueError(f"Unsupported target EPSG:{target_epsg}")

        gdf = self.to_gdf(assume_epsg).to_crs(epsg=target_epsg)

        out = self.from_gdf(gdf)
        out.crs = f"EPSG:{target_epsg}"
        out.style = self.style
        return out

    def to_epsg4326(self, assume_epsg: int | None = None) -> "GeoJSON":
        return self.to_crs(4326, assume_epsg)

    def to_epsg3857(self, assume_epsg: int | None = None) -> "GeoJSON":
        return self.to_crs(3857, assume_epsg)

    # -------------------------------------------------------
    # BUILDERS
    # -------------------------------------------------------
    @classmethod
    def from_gdf(cls, gdf: GeoDataFrame) -> "GeoJSON":
        # features = []
        #
        # for _, row in gdf.iterrows():
        #     geom = row.geometry
        #     if geom is None or geom.is_empty:
        #         continue
        #
        #     props = row.drop(labels=["geometry"]).to_dict()
        #     features.append(Feature.from_shapely(geom, props))
        crs_val = None
        if gdf.crs:
            epsg = gdf.crs.to_epsg()
            if epsg:
                crs_val = f"EPSG:{epsg}"

        return cls(**gdf.__geo_interface__, crs=crs_val)

    # -------------------------------------------------------
    # UTILITIES
    # -------------------------------------------------------
    def remove_duplicate_features_wkb(
        self,
        by_properties: bool = True,
        ignore_property_keys: Set[str] | None = None,
    ) -> "GeoJSON":
        ignore_property_keys = ignore_property_keys or set()

        seen = set()
        unique = []

        for f in self.features:
            geom = f.to_shapely()

            if not geom.is_valid:
                geom = geom.buffer(0)

            geom_key = geom.wkb_hex

            props = f.properties or {}
            props = {k: v for k, v in props.items() if k not in ignore_property_keys}

            key = (geom_key, json.dumps(props, sort_keys=True, default=str))

            if key in seen:
                continue

            seen.add(key)
            unique.append(f)

        return GeoJSON(
            features=unique,
            crs=self.crs,
            style=self.style,
        )