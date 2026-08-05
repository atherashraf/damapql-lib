import hashlib
import json
import math
import re

import geopandas as gpd
import mapbox_vector_tile
import mercantile
import numpy as np
import shapely
from shapely import transform
from shapely.geometry import box, LineString, Point, MultiLineString, MultiPoint, mapping, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.models import LayerInfo
from app.damapql.utils.adapters.db import DBBase
from digitalarzengine.io.managers.db_manager import GeoDBManager
from digitalarzengine.utils.singletons import da_logger


class MVTUtils:
    """
      https://github.com/tilezen/mapbox-vector-tile
    """
    SRID_SPHERICAL_MERCATOR = 3857
    MVT_EXTENT = 4096

    @classmethod
    def tile_gdf(cls, envelop_gdf: gpd.GeoDataFrame, geom_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        envelop_gdf.to_crs(cls.SRID_SPHERICAL_MERCATOR, inplace=True)
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
            tile_coord = (int((x_merc - x0) * cls.MVT_EXTENT / x_span), int((y_merc - y0) * cls.MVT_EXTENT / y_span))
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

    @classmethod
    def extent_to_xyz(cls, extent: list, z: int = 18):
        x1, y1, z1 = tuple(mercantile.tile(extent[0], extent[1], z))
        x2, y2, z2 = tuple(mercantile.tile(extent[2], extent[3], z))
        return x1, y1, x2, y2

    @classmethod
    def extent_to_xyz_tiles(cls, extent: list, z):
        return mercantile.tiles(*extent, z)

    @classmethod
    def xyz_to_extent_4326(cls, x: int, y: int, z: int):
        tile_xyz = (x, y, z)
        extent = mercantile.bounds(*tile_xyz)
        return extent

    @classmethod
    def xyz_to_env(cls, x: int, y: int, z: int) -> gpd.GeoDataFrame:
        extent = cls.xyz_to_extent_4326(x, y, z)
        env = box(*extent)
        gdf = gpd.GeoDataFrame(geometry=[env], crs=4326)
        return gdf

    @classmethod
    def xyz_to_extent_3857(cls, x: int, y: int, z: int):
        gdf = cls.xyz_to_env(x, y, z)
        gdf.to_crs(epsg=3857)
        return gdf.bounds

    @classmethod
    def get_pg_query(cls, table_name, geom_field, fields, x, y, z, srid):
        query = 'WITH mvtgeom AS (' \
                'SELECT ST_AsMVTGeom("%s", ' \
                'ST_TileEnvelope(%s, %s, %s), extent => 4096, buffer => 64) ' \
                'AS geom, %s ' \
                'FROM "%s" ' \
                'WHERE "%s" && ST_TileEnvelope(%s, %s, %s)' \
                ') SELECT ST_AsMVT(mvtgeom.*) FROM mvtgeom' % (geom_field, z, x, y, fields, table_name, geom_field, z,
                                                               x, y)
        da_logger.debug(query)
        return query

    @staticmethod
    def get_simplified_geom_col(g_col: str, srid: int, resolution: float) -> str:
        g = f'st_transform("{g_col}", 3857)' if srid != 3857 else f'"{g_col}"'
        g = f"st_collectionextract({g}, 3)"
        return f"st_simplify({g}, {resolution})"

    @classmethod
    def get_pg_mvt(cls, info: LayerInfo, connection: GeoDBManager, x: int, y: int, z: int, resolution: float = 0):
        layer_access = info.layer_accessibility
        table_name = f'"{layer_access.table_name}"'
        pk_col = info.pk_cols_as_pk

        geom_col = layer_access.geometry_col
        safe_cols_list = [f'"{c}"' for c in layer_access.cols_name if
            c != geom_col and c.lower() != 'geom' and c.lower() != 'pk']
        cols = ", ".join(safe_cols_list) if safe_cols_list else ""

        geom_transform = (
            f'ST_Simplify(ST_Transform("{geom_col}", 3857), {resolution})' if resolution > 0 else f'ST_Transform("{geom_col}", 3857)')

        g_select_attrs = (", " + cols) if cols else ""
        mvtgeom_select_attrs = (", " + cols) if cols else ""

        query = f"""
        WITH g AS (
            SELECT
                {pk_col},
                {geom_transform} AS geom_3857
                {g_select_attrs}
            FROM {table_name}
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
        bounds = (0., 0., 4096., 4096.)
        (x0, y0, x_max, y_max) = tile_extent
        P = np.array([[x0, x0, x_max], [y0, y_max, y_max], [1, 1, 1]])
        Pd = np.array([[bounds[0], bounds[0], bounds[2]], [bounds[1], bounds[3], bounds[3]], [1, 1, 1]])
        A = np.matmul(Pd, np.linalg.inv(P))
        A = A.reshape(-1)
        a = [A[0], A[1], A[3], A[4], A[2], A[5]]
        return a

    @classmethod
    def tile_transform_geometry(cls, gdf: gpd.GeoDataFrame, tile_extent: list):
        A = cls.get_title_affine_matrix(tile_extent)
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
        gdf = gdf[gdf[g_col].notnull()]
        return gdf


    @classmethod
    def gdf_to_mvt(cls, gdf: gpd.GeoDataFrame, tile_extent_3857: tuple, name: str) -> bytes:
        if gdf.empty:
            return b''

        if gdf.crs is None or gdf.crs.to_epsg() != 3857:
            gdf = gdf.to_crs(epsg=3857)

        tile_gdf = cls.transform_geometry_to_tile_space(gdf, tile_extent_3857, tile_extent=4096)
        geometry_column = tile_gdf.geometry.name
        features = []

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

                    def clean_val(v):
                        if v is None or (isinstance(v, float) and math.isnan(v)):
                            return ""  # Or None, if you prefer, but "" keeps the key in JS clients
                        if isinstance(v, (np.integer, np.floating)):
                            return v.item()
                        return str(v) if not isinstance(v, (str, int, float, bool)) else v

                    features.append({"geometry": geom,
                        "properties": {k: clean_val(v) for k, v in row.items() if k != geometry_column}})

                except Exception as e:
                    da_logger.exception(f"Failed to process geometry: {e}")

        if features:
            return mapbox_vector_tile.encode({"name": name, "features": features}, y_coord_down=True, extents=4096)
        else:
            return b''

    @classmethod
    def mvt_to_gdf(cls, mvt_bytes: bytes, layer_name: str = None, tile_extent_3857: tuple = None,
            y_coord_down: bool = True) -> gpd.GeoDataFrame:
        """
        Decodes raw MVT (Mapbox Vector Tile) bytes into a GeoPandas GeoDataFrame.

        :param mvt_bytes: The raw MVT binary content.
        :param layer_name: Optional name of the layer to extract. If None, decodes the first layer found.
        :param tile_extent_3857: Tuple of (minx, miny, maxx, maxy) in EPSG:3857.
                                  If provided, coordinates are transformed from local tile space to EPSG:3857.
                                  If None, geometries remain in MVT tile pixel coordinates (0 to 4096).
        :param y_coord_down: Standard MVT layout has Y pointing downward (top-left origin).
        :return: A GeoDataFrame with parsed geometry and feature properties.
        """
        if not mvt_bytes:
            return gpd.GeoDataFrame()

        # 1. Decode MVT binary data
        tile_data = mapbox_vector_tile.decode(mvt_bytes, y_coord_down=y_coord_down)

        if not tile_data:
            return gpd.GeoDataFrame()

        # 2. Extract requested layer or target the first available layer
        if layer_name:
            if layer_name not in tile_data:
                da_logger.warning(
                    f"Layer '{layer_name}' not found in MVT data. Available layers: {list(tile_data.keys())}")
                return gpd.GeoDataFrame()
            target_layer = tile_data[layer_name]
        else:
            first_key = next(iter(tile_data))
            target_layer = tile_data[first_key]

        features = target_layer.get("features", [])
        if not features:
            return gpd.GeoDataFrame()

        # 3. Reconstruct Shapely Geometries and Properties
        records = []
        for feat in features:
            geom_dict = feat.get("geometry")
            properties = feat.get("properties", {}).copy()

            if "id" in feat and feat["id"] is not None:
                properties["mvt_id"] = feat["id"]

            try:
                # Convert GeoJSON-like dict to Shapely geometry
                geom = shape(geom_dict)
                properties["geometry"] = geom
                records.append(properties)
            except Exception as e:
                da_logger.exception(f"Failed to parse feature geometry in MVT decoding: {e}")

        if not records:
            return gpd.GeoDataFrame()

        gdf = gpd.GeoDataFrame(records)

        # 4. (Optional) Invert the Tile Space Transformation back to EPSG:3857
        if tile_extent_3857:
            minx, miny, maxx, maxy = tile_extent_3857
            extent_x = maxx - minx
            extent_y = maxy - miny

            def to_3857_coords(coords):
                x = coords[:, 0]
                y = coords[:, 1]

                # Reverse scaling and translation matching transform_geometry_to_tile_space
                wx = minx + (x / cls.MVT_EXTENT) * extent_x
                wy = maxy - (y / cls.MVT_EXTENT) * extent_y if y_coord_down else miny + (y / cls.MVT_EXTENT) * extent_y
                return np.column_stack([wx, wy])

            gdf.geometry = gdf.geometry.apply(lambda g: transform(g, to_3857_coords) if g else None)
            gdf.crs = f"EPSG:{cls.SRID_SPHERICAL_MERCATOR}"

        return gdf


class MVTCache:
    @staticmethod
    def ensure_cache_table(session, uuid: str):
        if not re.fullmatch(r"[A-Fa-f0-9\-]+", uuid):
            raise ValueError("Invalid uuid")

        clean_uuid = uuid.replace("-", "")
        table_name = f"{clean_uuid}_xyz"
        index_name = f"{table_name}_idx"

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
    def build_variant(cols: list[str] | None = None, resolution: float = 0, feature_count: int | None = None,
            extra: dict | None = None) -> str:
        normalized = {"cols": sorted(set(cols or [])), "resolution": resolution, }

        if feature_count is not None:
            normalized["feature_count"] = feature_count

        if extra:
            normalized.update(extra)

        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @classmethod
    def get_cached_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str):
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            return session.execute(text(f"SELECT mvt FROM {table} "
                                        f"WHERE x=:x AND y=:y AND z=:z AND variant=:variant"),
                {"x": x, "y": y, "z": z, "variant": variant, }, ).scalar_one_or_none()

    @classmethod
    def set_cached_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str, mvt_bytes: bytes):
        if not mvt_bytes:
            return

        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            session.execute(text(f"INSERT INTO {table} (x, y, z, variant, mvt) "
                                 f"VALUES (:x, :y, :z, :variant, :mvt) "
                                 f"ON CONFLICT (x, y, z, variant) "
                                 f"DO UPDATE SET mvt = EXCLUDED.mvt"),
                {"x": x, "y": y, "z": z, "variant": variant, "mvt": mvt_bytes, }, )
            session.commit()

    @classmethod
    def clear_layer_cache(cls, layer_uuid: str) -> int:
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            result = session.execute(text(f"DELETE FROM {table}"))
            session.commit()

            da_logger.info(f"Cleared {result.rowcount} tiles for layer {layer_uuid}")
            return result.rowcount or 0

    @classmethod
    def clear_variant_cache(cls, layer_uuid: str, variant: str) -> int:
        engine = cls.get_cache_engine()

        with Session(engine) as session:
            table = cls.get_cache_table(session, layer_uuid)

            result = session.execute(text(f"DELETE FROM {table} WHERE variant = :variant"), {"variant": variant}, )
            session.commit()

            da_logger.info(f"Cleared {result.rowcount} cached tiles for layer {layer_uuid}, variant={variant}")
            return result.rowcount or 0
