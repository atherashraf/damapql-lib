"""
GeoJSON Pydantic Model

Usage:
    GeoJSON(type="FeatureCollection", crs="EPSG:3857", features=features)
    GeoJSON(type="FeatureCollection", srs="EPSG:3857", features=features)

    g = GeoJSON(type="FeatureCollection", srs="EPSG:3857", features=[])

    g.model_dump()
    # {'type': 'FeatureCollection', 'features': [], 'crs': 'EPSG:3857', 'style': None}

    g.model_dump(by_alias=True)
    # {'type': 'FeatureCollection', 'features': [], 'srs': 'EPSG:3857', 'style': None}
"""

from __future__ import annotations

import json
from typing import Any, Optional, Set, Literal

from geopandas import GeoDataFrame
from pydantic import BaseModel, Field, field_validator, ConfigDict
from shapely.geometry import shape, mapping
from shapely.geometry.base import BaseGeometry


ALLOWED_EPSG = {32642, 32643, 4326, 3857}


# -------------------------------------------------------
# Geometry Model
# -------------------------------------------------------
class GeometryData(BaseModel):
    """
    Represents a GeoJSON geometry object.

    Example:
        {"type": "MultiPolygon", "coordinates": [...]}
    """

    model_config = ConfigDict(from_attributes=True)

    type: str
    coordinates: Any

    def to_shapely(self) -> BaseGeometry:
        """
        Convert this geometry into a Shapely geometry.
        """
        return shape(self.model_dump())

    def to_dict(self) -> dict[str, Any]:
        """
        Return geometry as a plain GeoJSON-compatible dictionary.
        """
        return self.model_dump()

    @classmethod
    def from_shapely(cls, geom: BaseGeometry) -> "GeometryData":
        """
        Build GeometryData from a Shapely geometry.
        """
        return cls(**mapping(geom))


# -------------------------------------------------------
# Feature Model
# -------------------------------------------------------
class Feature(BaseModel):
    """
    Represents a GeoJSON Feature.

    A Feature contains:
    - geometry
    - properties

    Best conversions:
    - geometry only  -> to_shapely()
    - full feature   -> to_feature_dict()
    - tabular row    -> to_gdf()
    """

    model_config = ConfigDict(from_attributes=True)

    type: Literal["Feature"] = "Feature"
    geometry: GeometryData
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("geometry", mode="before")
    @classmethod
    def _coerce_geometry(cls, v):
        """
        Accept both GeometryData and raw GeoJSON geometry dict.
        Internally store GeometryData.
        """
        if isinstance(v, GeometryData):
            return v
        if isinstance(v, dict):
            return GeometryData(**v)
        raise TypeError("geometry must be a GeometryData instance or GeoJSON dict")

    def geometry_dict(self) -> dict[str, Any]:
        """
        Return geometry as a plain GeoJSON-compatible dictionary.
        """
        return self.geometry.to_dict()

    def to_shapely(self) -> BaseGeometry:
        """
        Convert feature geometry to a Shapely geometry object.
        """
        return self.geometry.to_shapely()

    def to_feature_dict(self) -> dict[str, Any]:
        """
        Return the full GeoJSON Feature as a dictionary.
        """
        return {
            "type": self.type,
            "geometry": self.geometry_dict(),
            "properties": self.properties or {},
        }

    def to_gdf(self, crs: str | None = None) -> GeoDataFrame:
        """
        Convert this feature into a one-row GeoDataFrame.
        """
        return GeoDataFrame(
            [self.properties or {}],
            geometry=[self.to_shapely()],
            crs=crs,
        )

    @classmethod
    def from_shapely(
        cls,
        geom: BaseGeometry,
        properties: dict[str, Any] | None = None,
    ) -> "Feature":
        """
        Build a Feature from a Shapely geometry and optional properties.
        """
        return cls(
            geometry=GeometryData.from_shapely(geom),
            properties=properties or {},
        )


# -------------------------------------------------------
# GeoJSON Model
# -------------------------------------------------------
class GeoJSON(BaseModel):
    """
    Pydantic model for GeoJSON FeatureCollection.

    Features:
    - Accepts CRS as `crs` or alias `srs`
    - Validates CRS format and allowed EPSG codes
    - Converts to/from GeoDataFrame
    - Supports reprojection
    - Removes duplicate features using geometry + properties
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    type: Literal["FeatureCollection"] = "FeatureCollection"

    features: list[Feature] = Field(
        default_factory=list,
        description="List of GeoJSON features",
    )

    # internal = crs, external alias = srs
    crs: str | None = Field(
        None,
        alias="srs",
        description="CRS in 'EPSG:<code>' format",
    )

    style: dict[str, Any] | None = Field(
        None,
        description="Optional styling metadata",
    )

    # -------------------------------------------------------
    # CRS VALIDATION
    # -------------------------------------------------------
    @field_validator("crs", mode="before")
    @classmethod
    def _coerce_crs_to_epsg_format(cls, v):
        """
        Normalize CRS to 'EPSG:<code>' format and validate.

        Accepts:
            - 4326
            - "4326"
            - "EPSG:4326"
        """
        if v is None:
            return None

        if isinstance(v, int):
            epsg = v
        elif isinstance(v, str):
            value = v.strip()
            if value.upper().startswith("EPSG:"):
                value = value.split(":", 1)[1]

            try:
                epsg = int(value)
            except ValueError as e:
                raise ValueError(f"Invalid CRS value: {v}") from e
        else:
            raise TypeError(f"Invalid CRS type: {type(v).__name__}")

        if epsg not in ALLOWED_EPSG:
            raise ValueError(f"Unsupported EPSG:{epsg}")

        return f"EPSG:{epsg}"

    def _get_source_epsg(self, assume_epsg: int | None = None) -> int:
        """
        Determine CRS as integer EPSG code.

        Priority:
        1. self.crs
        2. assume_epsg
        """
        if self.crs:
            return int(self.crs.split(":")[1])

        if assume_epsg is not None:
            if assume_epsg not in ALLOWED_EPSG:
                raise ValueError(f"Unsupported assumed CRS: {assume_epsg}")
            return assume_epsg

        raise ValueError("CRS unknown. Provide crs/srs or assume_epsg.")

    # -------------------------------------------------------
    # CONVERSIONS
    # -------------------------------------------------------
    def to_feature_collection_dict(self) -> dict[str, Any]:
        """
        Return the full FeatureCollection as a plain dictionary.
        """
        return {
            "type": self.type,
            "features": [f.to_feature_dict() for f in self.features],
            "crs": self.crs,
            "style": self.style,
        }

    def to_gdf(self, assume_epsg: int | None = None) -> GeoDataFrame:
        """
        Convert GeoJSON -> GeoDataFrame.

        Invalid or empty geometries are skipped.
        """
        geometries: list[BaseGeometry] = []
        properties: list[dict[str, Any]] = []

        for feature in self.features:
            try:
                geom = feature.to_shapely()
                if geom.is_empty:
                    continue
                geometries.append(geom)
                properties.append(feature.properties or {})
            except Exception:
                continue

        gdf = GeoDataFrame(properties, geometry=geometries)

        epsg = self._get_source_epsg(assume_epsg)
        gdf.set_crs(epsg=epsg, inplace=True, allow_override=True)

        return gdf

    def to_crs(self, target_epsg: int, assume_epsg: int | None = None) -> "GeoJSON":
        """
        Reproject to target CRS.
        """
        if target_epsg not in ALLOWED_EPSG:
            raise ValueError(f"Unsupported target EPSG:{target_epsg}")

        gdf = self.to_gdf(assume_epsg)
        gdf = gdf.to_crs(epsg=target_epsg)

        out = self.__class__.from_gdf(gdf)
        out.crs = f"EPSG:{target_epsg}"
        out.style = self.style
        return out

    def to_epsg4326(self, assume_epsg: int | None = None) -> "GeoJSON":
        """
        Reproject to EPSG:4326.
        """
        return self.to_crs(4326, assume_epsg)

    def to_epsg3857(self, assume_epsg: int | None = None, limit: int | None = None) -> "GeoJSON":
        """
        Reproject to EPSG:3857.

        Optionally sample a limited number of features before reprojection.
        """
        gdf = self.to_gdf(assume_epsg)

        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be greater than 0")
            if len(gdf) > limit:
                gdf = gdf.sample(n=limit)

        gdf = gdf.to_crs(epsg=3857)

        out = self.__class__.from_gdf(gdf)
        out.crs = "EPSG:3857"
        out.style = self.style
        return out

    # -------------------------------------------------------
    # BUILD FROM GDF
    # -------------------------------------------------------
    @classmethod
    def from_gdf(cls, gdf: GeoDataFrame) -> "GeoJSON":
        """
        Convert GeoDataFrame -> GeoJSON model.
        """
        features: list[Feature] = []

        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            props = row.drop(labels=["geometry"]).to_dict()
            features.append(
                Feature.from_shapely(geom, properties=props)
            )

        crs_val = None
        if gdf.crs:
            try:
                epsg = gdf.crs.to_epsg()
                if epsg:
                    crs_val = f"EPSG:{epsg}"
            except Exception:
                pass

        return cls(
            type="FeatureCollection",
            features=features,
            crs=crs_val,
        )

    @classmethod
    def from_features(
        cls,
        features: list[Feature],
        crs: str | int | None = None,
        style: dict[str, Any] | None = None,
    ) -> "GeoJSON":
        """
        Build GeoJSON directly from a list of Feature objects.
        """
        return cls(
            type="FeatureCollection",
            features=features,
            crs=crs,
            style=style,
        )

    @classmethod
    def get_default_geojson(cls) -> "GeoJSON":
        """
        Empty GeoJSON template.
        """
        return cls(type="FeatureCollection", features=[])

    # -------------------------------------------------------
    # UTILITIES
    # -------------------------------------------------------
    def remove_duplicate_features_wkb(
        self,
        by_properties: bool = True,
        ignore_property_keys: Set[str] | None = None,
        only_property_keys: Set[str] | None = None,
    ) -> "GeoJSON":
        """
        Remove duplicate features using geometry WKB and optionally properties.
        """
        ignore_property_keys = ignore_property_keys or set()

        seen = set()
        unique_features: list[Feature] = []

        for feature in self.features:
            geom = feature.to_shapely()

            if not geom.is_valid:
                geom = geom.buffer(0)

            geom_key = geom.wkb_hex

            props = feature.properties or {}
            props_key = None

            if by_properties:
                if only_property_keys:
                    props = {k: props.get(k) for k in only_property_keys}
                else:
                    props = {k: v for k, v in props.items() if k not in ignore_property_keys}

                props_key = json.dumps(props, sort_keys=True, default=str)

            dedup_key = (geom_key, props_key)

            if dedup_key in seen:
                continue

            seen.add(dedup_key)
            unique_features.append(feature)

        return self.__class__(
            type=self.type,
            features=unique_features,
            crs=self.crs,
            style=self.style,
        )

    def model_dump_geojson(self, by_alias: bool = False) -> dict[str, Any]:
        """
        Dump as GeoJSON-like dict with optional alias support for `srs`.
        """
        return self.model_dump(by_alias=by_alias)

    def model_dump_json_geojson(self, by_alias: bool = False, **kwargs) -> str:
        """
        Dump as JSON string.
        """
        return self.model_dump_json(by_alias=by_alias, **kwargs)