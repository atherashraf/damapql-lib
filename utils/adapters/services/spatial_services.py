"""
Spatial Service Layer (spatial_service.py)
---------------------------------------
Extends BaseService with PostGIS spatial analysis capabilities.

USAGE:
    class ParcelService(SpatialService[Parcel]):
        ...
    parcel_service = ParcelService(Parcel)
"""


from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List

from utils.adapters.services.base_services import BaseService, T


class SpatialService(BaseService[T]):

    def get_by_major_overlap(
            self,
            db: Session,
            aoi_wkt: str,
            threshold: float = 0.8,
            srid: int = 4326,
            geom_col_name: str = "geom"
    ) -> List[T]:
        """
        Fetches records where the intersection with the provided AOI
        covers at least a specific ratio (default 80%) of the record's area.

        @param threshold: Minimum ratio of overlap (0.0 to 1.0).
        """
        geom_col = getattr(self.model, geom_col_name)
        db_srid = geom_col.type.srid

        # 1. Prepare and Transform AOI
        aoi_geom = func.ST_GeomFromText(aoi_wkt, srid)
        if srid != db_srid:
            aoi_geom = func.ST_Transform(aoi_geom, db_srid)

        # 2. Optimized Query:
        # ST_Intersects uses the GIST index, ST_Area handles the calculation.
        intersection_area = func.ST_Area(func.ST_Intersection(geom_col, aoi_geom))
        total_area = func.ST_Area(geom_col)

        return db.query(self.model).filter(
            func.ST_Intersects(geom_col, aoi_geom),
            total_area > 0,
            (intersection_area / total_area) >= threshold
        ).all()

    def get_within_radius(
            self,
            db: Session,
            lat: float,
            lon: float,
            radius_meters: float,
            geom_col_name: str = "geom"
    ) -> List[T]:
        """
        Fetches records within a radius of a point. Uses ST_DWithin for index efficiency.
        """
        geom_col = getattr(self.model, geom_col_name)
        db_srid = geom_col.type.srid

        point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
        if db_srid != 4326:
            point = func.ST_Transform(point, db_srid)

        return db.query(self.model).filter(
            func.ST_DWithin(geom_col, point, radius_meters)
        ).all()