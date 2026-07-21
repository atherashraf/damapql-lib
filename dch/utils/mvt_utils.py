import hashlib
import json
import math
import re

import mercantile as mercantile
import geopandas as gpd
import numpy as np
import shapely
from digitalarzengine.io.managers.db_manager import GeoDBManager
from digitalarzengine.utils.singletons import da_logger
from shapely import transform
from shapely.geometry import box, LineString, Point, MultiLineString, MultiPoint, mapping
import mapbox_vector_tile
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.models import LayerInfo
from app.damapql.utils.adapters.db import DBBase


class MVTUtils:
    """
      https://github.com/tilezen/mapbox-vector-tile
    """
    SRID_SPHERICAL_MERCATOR = 3857
    # `mapbox-vector-tile` has a hardcoded tile extent of 4096 units.
    MVT_EXTENT = 4096

    @classmethod
    def tile_gdf(cls, envelop_gdf: gpd.GeoDataFrame, geom_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:

        # We need tile bounds in spherical mercator
        # assert tile_bounds.srid == cls.SRID_SPHERICAL_MERCATOR
        envelop_gdf.to_crs(cls.SRID_SPHERICAL_MERCATOR, inplace=True)

        # And we need the line to be in a known projection so we can re-project
        assert geom_gdf.crs is not None
        geom_gdf.to_crs(cls.SRID_SPHERICAL_MERCATOR, inplace=True)

        (x0, y0, x_max, y_max) = envelop_gdf.total_bounds
        x_span = x_max - x0
        y_span = y_max - y0

        def geom_to_tile(geom):
            if isinstance(geom, shapely.geometry.Point):
                tile_coord = to_tile_coord(geom)
                return Point(tile_coord[0], tile_coord[1])
            elif isinstance(geom, shapely.geometry.MultiPoint):
                points: MultiPoint = geom
                tile_coords = [to_tile_coord(coord) for coord in list(points.coords)]
                return MultiPoint(tile_coords)
            elif isinstance(geom, shapely.geometry.LineString):
                line: LineString = geom
                tile_coords = [to_tile_coord(coord) for coord in list(line.coords)]
                return LineString(tile_coords)
            elif isinstance(geom, shapely.geometry.MultiLineString):
                multiLine: MultiLineString = geom
                tile_coord = [[to_tile_coord(coord) for coord in list(line.coords)] for line in multiLine]
                return MultiLineString(tile_coord)

        def to_tile_coord(coord):
            x_merc, y_merc = coord.x, coord.y
            tile_coord = (int((x_merc - x0) * cls.MVT_EXTENT / x_span),
                          int((y_merc - y0) * cls.MVT_EXTENT / y_span))
            return tile_coord

        geom_gdf['geom'] = geom_gdf['geom'].apply(lambda geom: geom_to_tile(geom))
        return geom_gdf

    @classmethod
    def to_lat_lon(cls, x, y, z):
        n = 2.0 ** z
        lon_deg = x / n * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
        lat_deg = math.degrees(lat_rad)
        return lon_deg, lat_deg

    # @classmethod
    # def to_extent_4326(cls, x: int, y: int, z: int):
    #     n = math.pow(2, z)
    #     left_lon_deg = x / n * 360.0 - 180.0
    #     right_lon_deg = (x + 1) / n * 360.0 - 180.0
    #     top_lat_deg = (math.atan(math.sinh(math.pi * (1 - 2 * y / n)))) * 180 / math.pi
    #     bottom_lat_deg = (math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n)))) * 180 / math.pi
    #     gm = GlobalMercator()
    #     res = gm.TileLatLonBounds(x, y, z)
    #     return [res[1], res[0], res[3], res[2]]

    @classmethod
    def extent_to_xyz(cls, extent: list, z: int = 18):
        # gm = GlobalMercator()
        # x1, y1 = gm.MetersToTile(extent[0], extent[1], z)
        # x2, y2 = gm.MetersToTile(extent[2], extent[3], z)
        x1, y1, z1 = tuple(mercantile.tile(extent[0], extent[1], z))
        x2, y2, z2 = tuple(mercantile.tile(extent[2], extent[3], z))
        return x1, y1, x2, y2

    @classmethod
    def extent_to_xyz_tiles(cls, extent: list, z):
        """
        :param extent:  min_x,min_y, max_x, max_y in geographic coordinates 4326
        :param z: zoom level
        :return: array of tuple (x,y,z)
        """
        tiles = mercantile.tiles(*extent, z)
        # res = []
        # for tile in tiles:
        #     res.append(tuple(tile))
        return tiles

    @classmethod
    def xyz_to_extent_4326(cls, x: int, y: int, z: int):
        tile_xyz = (x, y, z)
        # lat, long = cls.to_lat_lon(*tile_xyz)
        extent = mercantile.bounds(*tile_xyz)
        return extent

    @classmethod
    def xyz_to_env(cls, x: int, y: int, z: int) -> gpd.GeoDataFrame:
        """
        return gdf in 4326
        :param x:
        :param y:
        :param z:
        :return:
        """
        # query = f"Select st_extent(ST_TileEnvelope({z}, {x},{y}))"
        # manager = GeoDBManager("default_local")
        # extent = manager.execute_query_as_one(query)
        extent = cls.xyz_to_extent_4326(x, y, z)
        env = box(*extent)
        gdf = gpd.GeoDataFrame(geometry=[env], crs=4326)
        return gdf

    @classmethod
    def xyz_to_extent_3857(cls, x: int, y: int, z: int):
        gdf = cls.xyz_to_env(x, y, z)
        gdf.to_crs(epsg=3857)
        return gdf.bounds

        # return [min_x + buff_size, min_y + buff_size, max_x + buff_size, max_y + buff_size]

    @classmethod
    def get_pg_query(cls, table_name, geom_field, fields, x, y, z, srid):
        query = 'WITH mvtgeom AS (' \
                'SELECT ST_AsMVTGeom("%s", ' \
                'ST_TileEnvelope(%s, %s, %s), extent => 4096, buffer => 64) ' \
                'AS geom, %s ' \
                'FROM "%s" ' \
                'WHERE %s && ST_TileEnvelope(%s, %s, %s)' \
                ') SELECT ST_AsMVT(mvtgeom.*) FROM mvtgeom' % (
                    geom_field, z, x, y, fields, table_name, geom_field, z, x, y)
        da_logger.debug(query)
        return query

    @staticmethod
    def get_simplified_geom_col(g_col: str, srid: int, resolution: float) -> str:
        """
        Returns safe, simplified geometry expression with SRID normalization.
        """
        g = f"st_transform({g_col}, 3857)" if srid != 3857 else g_col
        g = f"st_collectionextract({g}, 3)"  # Safe for MultiSurface, GeometryCollection
        return f"st_simplify({g}, {resolution})"

    @classmethod
    def get_pg_mvt(cls, info: LayerInfo, connection: GeoDBManager, x: int, y: int, z: int, resolution: float = 0):
        layer_access = info.layer_accessibility
        table_name = f'"{layer_access.table_name}"'  # Ensure table name is quoted
        pk_col = info.pk_cols_as_pk  # e.g., 'id as pk'

        geom_col = layer_access.geometry_col  # e.g., 'geom'
        # Exclude geometry and 'pk' alias collisions from the attribute list
        safe_cols_list = [
            c for c in layer_access.cols_name
            if c != geom_col and c.lower() != 'geom' and c.lower() != 'pk'
        ]
        cols = ", ".join(f'"{col}"' for col in safe_cols_list) if safe_cols_list else ""

        # Geometry transformation (simplify only if resolution > 0)
        geom_transform = (
            f'ST_Simplify(ST_Transform("{geom_col}", 3857), {resolution})'
            if resolution > 0
            else f'ST_Transform("{geom_col}", 3857)'
        )

        # Build the SELECT column list pieces
        g_select_attrs = (", " + cols) if cols else ""
        mvtgeom_select_attrs = (", " + cols) if cols else ""

        query = f"""
        WITH g AS (
            SELECT
                {pk_col},
                {geom_transform} AS geom_3857
                {g_select_attrs}
            FROM {table_name}
            -- IMPORTANT: refer to the source geometry column here, not the alias
            WHERE GeometryType("{geom_col}") != 'MULTISURFACE'
        ),
        mvtgeom AS (
            SELECT
                pk,
                ST_AsMVTGeom(
                    geom_3857,
                    ST_TileEnvelope({z}, {x}, {y}),
                    extent => 4096,
                    buffer => 64
                ) AS geom
                {mvtgeom_select_attrs}
            FROM g
            WHERE geom_3857 && ST_TileEnvelope({z}, {x}, {y}, margin => (64.0 / 4096))
        )
        SELECT ST_AsMVT(mvtgeom, 'layer_name', 4096, 'geom')
        FROM mvtgeom
        WHERE geom IS NOT NULL;
        """

        with connection.managed_session() as session:
            mvt_bytes = session.execute(text(query)).scalar()

        return bytes(mvt_bytes or b"")

    @classmethod
    def get_title_affine_matrix(cls, tile_extent: list):
        """
            return affine in the form of [a, b, d, e, xoff, yoff]

        """
        # scale = 4096
        bounds = (0., 0., 4096., 4096.)
        (x0, y0, x_max, y_max) = tile_extent
        P = np.array([[x0, x0, x_max], [y0, y_max, y_max], [1, 1, 1]])
        Pd = np.array([[bounds[0], bounds[0], bounds[2]], [bounds[1], bounds[3], bounds[3]], [1, 1, 1]])
        A = np.matmul(Pd, np.linalg.inv(P))
        A = A.reshape(-1)
        # [a, b, d, e, xoff, yoff]
        a = [A[0], A[1], A[3], A[4], A[2], A[5]]
        return a

    @classmethod
    def tile_transform_geometry(cls, gdf: gpd.GeoDataFrame, tile_extent: list):
        A = cls.get_title_affine_matrix(tile_extent)
        # simpledec = re.compile(r"\d*\.\d+")

        # def m_round(match):
        #     return "{:.1f}".format(float(match.group()))
        #
        # def apply_affine(wkt):
        #     geom = loads(re.sub(simpledec, m_round, wkt))
        #     return affine_transform(geom, A)

        # geoms = gdf.geometry.apply(lambda x: loads(re.sub(simpledec, mround, x.wkt)))
        # geoms = gdf.geometry.apply(lambda x: apply_affine(x.wkt))
        # gdf = gdf.explode(ignore_index=True)
        g = gdf.geometry.affine_transform(A)
        return g

    @classmethod
    def transform_geometry_to_tile_space(cls, gdf: gpd.GeoDataFrame, extent_3857: tuple,
                                         tile_extent: int = 4096) -> gpd.GeoDataFrame:
        minx, miny, maxx, maxy = extent_3857
        scale_x = tile_extent / (maxx - minx)
        scale_y = tile_extent / (maxy - miny)

        def to_tile_coords(coords) -> np.ndarray:
            x = coords[:, 0]
            y = coords[:, 1]
            tx = (x - minx) * scale_x
            ty = (maxy - y) * scale_y

            # Round and convert to integer tile coordinates
            return np.column_stack([tx, ty])

        def safe_transform(geom):
            if not isinstance(geom, BaseGeometry):
                da_logger.error(f"❌ Invalid geometry type: {type(geom)} — value: {repr(geom)}")
                return None
            if geom.is_empty:
                da_logger.warning("⚠️ Empty geometry found, skipping.")
                return None
            try:
                from shapely.validation import make_valid
                geom = make_valid(geom)
                trans_geom = transform(geom, to_tile_coords)
                return trans_geom
            except Exception as e:
                da_logger.exception("❌ Exception during transform:", str(e), repr(geom))
                return None

        gdf = gdf.copy()
        g_col = gdf.geometry.name
        gdf[g_col] = gdf.geometry.apply(safe_transform)
        gdf = gdf[gdf[g_col].notnull()]  # Filter out invalid results
        return gdf

    @classmethod
    def gdf_to_mvt(cls, gdf: gpd.GeoDataFrame, tile_extent_3857: tuple, name: str) -> bytes:
        """
        Converts a GeoDataFrame into a Mapbox Vector Tile (MVT) byte stream.

        :param gdf: GeoDataFrame containing features; can be in any CRS (will be reprojected to EPSG:3857)
        :param tile_extent_3857: Tuple (minx, miny, maxx, maxy) representing the tile's bounding box in EPSG:3857
                                 Typically retrieved from WebMercatorTiling(tile_size).tile_bounds(x, y, z)
        :param name: Name of the layer inside the MVT tile
        :return: Byte array representing the MVT tile
        """
        # Step 1: Return empty bytes if there's nothing to encode
        if gdf.empty:
            return b''

        # Step 2: Ensure GeoDataFrame is in Web Mercator (EPSG:3857), which is required for tile projection
        if gdf.crs is None or gdf.crs.to_epsg() != 3857:
            gdf = gdf.to_crs(epsg=3857)

        # Step 3: Transform geometries from world coordinates to local tile pixel space (0–4096 range)
        tile_gdf = cls.transform_geometry_to_tile_space(gdf, tile_extent_3857, tile_extent=4096)

        # Step 4: Identify the geometry column name
        geometry_column = tile_gdf.geometry.name

        # Step 5: Prepare feature list for the MVT encoder
        features = []

        # Define once outside the loop
        def round_coordinates(coords):
            if isinstance(coords, (list, tuple)):
                if isinstance(coords[0], (float, int)):
                    return [int(round(c)) for c in coords]
                else:
                    return [round_coordinates(c) for c in coords]
            else:
                raise ValueError("Unexpected coordinate format")

        for _, row in tile_gdf.iterrows():
            geom = row[geometry_column]
            if geom and not geom.is_empty:
                try:
                    geom = mapping(geom)
                    geom_type = geom['type']

                    if geom_type in ['Point', 'MultiPoint', 'LineString', 'Polygon', 'MultiPolygon', 'MultiLineString']:
                        geom['coordinates'] = round_coordinates(geom['coordinates'])
                    else:
                        raise NotImplementedError(f"Geometry type {geom_type} not supported.")

                    features.append({
                        "geometry": geom,
                        "properties": {
                            k: v for k, v in row.items() if k != geometry_column
                        }
                    })

                except Exception as e:
                    da_logger.exception(f"Failed to process geometry: {e}")

        # Step 6: Encode to MVT if we have at least one feature
        if features:
            per_layer_options = {
                name: {
                    "y_coord_down": True,  # instruct encoder to flip Y
                    # "extents": 4096,
                    # "quantize_bounds": (minx, miny, maxx, maxy),
                    # "on_invalid_geometry": on_invalid_geometry_make_valid
                }
            }
            # return mapbox_vector_tile.encode({
            #     "name": name,
            #     "features": features
            # }, per_layer_options=per_layer_options)
            return mapbox_vector_tile.encode({
                "name": name,
                "features": features
            },y_coord_down = True,extents=4096)
        else:
            return b''  # No features to encode → return empty tile




class MVTCache:
    @staticmethod
    def ensure_cache_table(session, uuid: str):
        if not re.fullmatch(r"[A-Fa-f0-9]+", uuid):
            raise ValueError("Invalid uuid")

        table_name = f"{uuid}_xyz"
        index_name = f"{table_name}_idx"

        # New schema: variant-aware cache key
        session.execute(text(f'''
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                z INTEGER NOT NULL,
                variant TEXT NOT NULL,
                mvt BLOB NOT NULL,
                PRIMARY KEY (x, y, z, variant)
            )
        '''))

        session.execute(text(f'''
            CREATE INDEX IF NOT EXISTS "{index_name}"
            ON "{table_name}" (z, x, y, variant)
        '''))

        session.commit()
        return f'"{table_name}"'

    @classmethod
    def get_cache_engine(cls):
        return DBBase.get_engine(db_key="cache_db")

    @classmethod
    def get_cache_table(cls, session, layer_uuid: str):
        return cls.ensure_cache_table(session, layer_uuid)

    @staticmethod
    def build_variant(
        cols: list[str] | None = None,
        resolution: float = 0,
        extra: dict | None = None
    ) -> str:
        """
        Build a stable cache variant hash from request-affecting parameters.

        Example:
            variant = MVTCache.build_variant(
                cols=["sewer_type", "diameter"],
                resolution=0,
                extra={"date": "2026-04-09"}
            )
        """
        normalized = {
            "cols": sorted(set(cols or [])),
            "resolution": resolution,
        }

        if extra:
            normalized.update(extra)

        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @classmethod
    def get_cached_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str):
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            return session.execute(
                text(
                    f"SELECT mvt FROM {table} "
                    f"WHERE x=:x AND y=:y AND z=:z AND variant=:variant"
                ),
                {
                    "x": x,
                    "y": y,
                    "z": z,
                    "variant": variant,
                },
            ).scalar_one_or_none()

    @classmethod
    def set_cached_tile(
        cls,
        layer_uuid: str,
        x: int,
        y: int,
        z: int,
        variant: str,
        mvt_bytes: bytes
    ):
        if not mvt_bytes:
            return

        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            session.execute(
                text(
                    f"INSERT INTO {table} (x, y, z, variant, mvt) "
                    f"VALUES (:x, :y, :z, :variant, :mvt) "
                    f"ON CONFLICT (x, y, z, variant) "
                    f"DO UPDATE SET mvt = EXCLUDED.mvt"
                ),
                {
                    "x": x,
                    "y": y,
                    "z": z,
                    "variant": variant,
                    "mvt": mvt_bytes,
                },
            )
            session.commit()

    @classmethod
    def clear_layer_cache(cls, layer_uuid: str) -> int:
        """
        Clears ALL cached tiles for a layer.
        """
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            result = session.execute(text(f"DELETE FROM {table}"))
            session.commit()

            da_logger.info(f"Cleared {result.rowcount} tiles for layer {layer_uuid}")
            return result.rowcount or 0

    @classmethod
    def clear_variant_cache(cls, layer_uuid: str, variant: str) -> int:
        """
        Clears cached tiles for a specific variant of a layer.
        """
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            result = session.execute(
                text(f"DELETE FROM {table} WHERE variant = :variant"),
                {"variant": variant},
            )
            session.commit()

            da_logger.info(
                f"Cleared {result.rowcount} cached tiles for layer {layer_uuid}, variant={variant}"
            )
            return result.rowcount or 0

