from math import pi, atan, exp, degrees
from pyproj import Transformer


class WebMercatorTiling:
    EARTH_RADIUS = 6378137
    EARTH_CIRCUMFERENCE = 2 * pi * EARTH_RADIUS
    INITIAL_RESOLUTION = EARTH_CIRCUMFERENCE / 256
    ORIGIN_SHIFT = EARTH_CIRCUMFERENCE / 2.0

    def __init__(self, tile_size: int = 256):
        self.tile_size = tile_size

    def resolution(self, zoom: int) -> float:
        return self.EARTH_CIRCUMFERENCE / (self.tile_size * 2**zoom)

    def tile_bounds(self, x: int, y: int, zoom: int) -> tuple:
        res = self.resolution(zoom)
        minx = x * self.tile_size * res - self.ORIGIN_SHIFT
        maxx = (x + 1) * self.tile_size * res - self.ORIGIN_SHIFT
        maxy = self.ORIGIN_SHIFT - y * self.tile_size * res
        miny = self.ORIGIN_SHIFT - (y + 1) * self.tile_size * res
        return minx, miny, maxx, maxy

    def meters_to_lon(self, mx: float) -> float:
        return degrees(mx / self.EARTH_RADIUS)

    def meters_to_lat(self, my: float) -> float:
        return degrees(2 * atan(exp(my / self.EARTH_RADIUS)) - pi / 2)

    def tile_bounds_latlon(self, x: int, y: int, zoom: int) -> tuple:
        minx, miny, maxx, maxy = self.tile_bounds(x, y, zoom)
        return (
            self.meters_to_lon(minx),
            self.meters_to_lat(miny),
            self.meters_to_lon(maxx),
            self.meters_to_lat(maxy)
        )

    def tile_bounds_projected(self, x: int, y: int, zoom: int, target_srid: int) -> tuple:
        """
        Returns bounding box of tile (x, y, zoom) transformed to a given projection (by SRID).
        Output: (minx, miny, maxx, maxy) in target projection.
        """
        minx, miny, maxx, maxy = self.tile_bounds(x, y, zoom)

        transformer = Transformer.from_crs("EPSG:3857", f"EPSG:{target_srid}", always_xy=True)

        minx_p, miny_p = transformer.transform(minx, miny)
        maxx_p, maxy_p = transformer.transform(maxx, maxy)

        return minx_p, miny_p, maxx_p, maxy_p
