import math
from typing import List, Optional, Tuple

from sqlalchemy import text

from settings import DEFAULT_EXTENT, DEFAULT_SRID


class SpatialAnalyzer:
    def __init__(self, default_extent=None):
        self.default_extent = list(default_extent or DEFAULT_EXTENT)
        self.default_srid = DEFAULT_SRID

    def get_srid_and_geom_type(
        self,
        engine,
        table_name: str,
        geom_col_name: str
    ) -> Tuple[Optional[int], List[str]]:
        srid = None
        geom_types = []

        if engine.dialect.name == "postgresql":
            try:
                sql = text(f'''
                    SELECT
                        ST_SRID("{geom_col_name}") AS srid,
                        GeometryType("{geom_col_name}") AS geom_type
                    FROM "{table_name}"
                    WHERE "{geom_col_name}" IS NOT NULL
                    LIMIT 50
                ''')

                with engine.connect() as conn:
                    rows = conn.execute(sql).fetchall()

                srids = {r[0] for r in rows if r[0] is not None}
                gtypes = {r[1] for r in rows if r[1]}

                if srids:
                    srid = list(srids)[0]
                if gtypes:
                    geom_types = sorted(list(gtypes))
            except Exception:
                pass

        return srid, geom_types

    @staticmethod
    def _is_valid_extent(extent) -> bool:

        if not extent or len(extent) != 4:
            return False

        for v in extent:
            if v is None:
                return False
            if not isinstance(v, (int, float)):
                return False
            if not math.isfinite(v):
                return False

        minx, miny, maxx, maxy = extent
        return minx <= maxx and miny <= maxy

    def get_table_extent(
            self,
            engine,
            table_name: str,
            geom_col_name: str,
            srid: Optional[int] = None
    ) -> List[float]:
        # if not srid:
        #     srid = self.default_srid

        if engine.dialect.name != "postgresql":
            return list(self.default_extent)

        try:
            sql = text(f'''
                SELECT
                    ST_XMin(ext) AS minx,
                    ST_YMin(ext) AS miny,
                    ST_XMax(ext) AS maxx,
                    ST_YMax(ext) AS maxy
                FROM (
                    SELECT ST_Extent("{geom_col_name}") AS ext
                    FROM "{table_name}"
                    WHERE "{geom_col_name}" IS NOT NULL
                ) q
            ''')

            with engine.connect() as conn:
                row = conn.execute(sql).fetchone()

            if not row:
                return list(self.default_extent)

            extent = [row[0], row[1], row[2], row[3]]

            if not self._is_valid_extent(extent):
                return list(self.default_extent)

            return [float(v) for v in extent]

        except Exception:
            return list(self.default_extent)

    def to_3857_extent(
            self,
            engine,
            extent: List[float],
            srid: Optional[int] = None
    ) -> List[float]:
        if not self._is_valid_extent(extent):
            return list(self.default_extent)

        if not srid:
            srid = self.default_srid

        if engine is None or engine.dialect.name != "postgresql":
            return list(extent)

        try:
            minx, miny, maxx, maxy = extent

            sql = text('''
                       SELECT ST_XMin(g) AS minx,
                              ST_YMin(g) AS miny,
                              ST_XMax(g) AS maxx,
                              ST_YMax(g) AS maxy
                       FROM (SELECT ST_Transform(
                                            ST_SetSRID(ST_MakeEnvelope(:minx, :miny, :maxx, :maxy), :srid),
                                            3857
                                    ) AS g) q
                       ''')

            with engine.connect() as conn:
                row = conn.execute(sql, {
                    "minx": minx,
                    "miny": miny,
                    "maxx": maxx,
                    "maxy": maxy,
                    "srid": srid,
                }).fetchone()

            if not row:
                return list(self.default_extent)

            transformed = [row[0], row[1], row[2], row[3]]

            if not self._is_valid_extent(transformed):
                return list(self.default_extent)

            return [float(v) for v in transformed]

        except Exception:
            return list(self.default_extent)

    def build_spatial_metadata(
            self,
            engine,
            table_name: str,
            geom_col_name: Optional[str]
    ) -> dict:
        if not geom_col_name:
            return {
                "srid": self.default_srid,
                "geom_type": [],
                "extent": list(self.default_extent),
                "extent_3857": self.to_3857_extent(
                    engine=engine,
                    extent=list(self.default_extent),
                    srid=self.default_srid,
                ),
            }

        detected_srid, detected_geom_types = self.get_srid_and_geom_type(
            engine=engine,
            table_name=table_name,
            geom_col_name=geom_col_name,
        )

        srid = detected_srid or self.default_srid
        extent = self.get_table_extent(
            engine=engine,
            table_name=table_name,
            geom_col_name=geom_col_name,
            srid=srid,
        )
        extent_3857 = self.to_3857_extent(
            engine=engine,
            extent=extent,
            srid=srid,
        )

        return {
            "srid": srid,
            "geom_type": detected_geom_types or [],
            "extent": extent,
            "extent_3857": extent_3857,
        }