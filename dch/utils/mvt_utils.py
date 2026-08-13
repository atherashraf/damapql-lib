import hashlib
import json
import math
import os

import geopandas as gpd
import mapbox_vector_tile
import mercantile
import numpy as np
import redis
import shapely
from digitalarzengine.io.managers.db_manager import GeoDBManager
from digitalarzengine.utils.singletons import da_logger
from shapely import transform
from shapely.geometry import box, LineString, Point, MultiLineString, MultiPoint, mapping, shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.damapql.dch.models import LayerInfo
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.db_mixin import EngineType

# -------------------------------------------------------------------
# REDIS CLIENT INITIALIZATION
# -------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=False,
        # CRITICAL: Preserves raw binary MVT byte stream
        socket_timeout=1.0,  # Keeps tile serving fast if Redis goes offline
        socket_connect_timeout=1.0, )
except Exception as e:
    redis_client = None
    da_logger.warning(f"⚠️ Redis client failed to initialize: {e}")


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
        """
        if not mvt_bytes:
            return gpd.GeoDataFrame()

        tile_data = mapbox_vector_tile.decode(mvt_bytes, y_coord_down=y_coord_down)

        if not tile_data:
            return gpd.GeoDataFrame()

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

        records = []
        for feat in features:
            geom_dict = feat.get("geometry")
            properties = feat.get("properties", {}).copy()

            if "id" in feat and feat["id"] is not None:
                properties["mvt_id"] = feat["id"]

            try:
                geom = shape(geom_dict)
                properties["geometry"] = geom
                records.append(properties)
            except Exception as e:
                da_logger.exception(f"Failed to parse feature geometry in MVT decoding: {e}")

        if not records:
            return gpd.GeoDataFrame()

        gdf = gpd.GeoDataFrame(records)

        if tile_extent_3857:
            minx, miny, maxx, maxy = tile_extent_3857
            extent_x = maxx - minx
            extent_y = maxy - miny

            def to_3857_coords(coords):
                x = coords[:, 0]
                y = coords[:, 1]
                wx = minx + (x / cls.MVT_EXTENT) * extent_x
                wy = maxy - (y / cls.MVT_EXTENT) * extent_y if y_coord_down else miny + (y / cls.MVT_EXTENT) * extent_y
                return np.column_stack([wx, wy])

            gdf.geometry = gdf.geometry.apply(lambda g: transform(g, to_3857_coords) if g else None)
            gdf.crs = f"EPSG:{cls.SRID_SPHERICAL_MERCATOR}"

        return gdf

    @classmethod
    def get_tile_bytes(cls, model_cls: type, x: int, y: int, z: int, layer_name: str | None = None,
                       requested_cols: list[str] | None = None, geom_col: str | None = None, extent: int = 4096,
                       buffer: int = 64, resolution: float = 0, session: Session | None = None) -> bytes:
        """
        Engine-aware MVT tile generator.

        - If Engine is PostgreSQL: Executes native ST_AsMVT pipeline in database kernel.
        - If Engine is SQLite/SpatiaLite/Other: Falls back to Python (mercantile + GeoPandas + mapbox_vector_tile).
        """
        engine = model_cls.get_engine()
        engine_type = EngineType.from_dialect(engine.dialect.name)

        layer_name = layer_name or model_cls.__name__
        target_table = model_cls.__tablename__

        detected_geom, srid = model_cls.get_geom_col_name_srid()
        target_geom = geom_col or detected_geom or "geom"
        srid = srid or 4326

        pk_fields = model_cls.get_primary_key_fields()
        all_cols = model_cls.get_fields(skip_fields=[target_geom])

        if requested_cols:
            safe_cols = [c for c in requested_cols if c in all_cols and c not in pk_fields]
        else:
            safe_cols = [c for c in all_cols if c not in pk_fields]

        if engine_type == EngineType.POSTGRESQL:
            return cls._get_postgis_mvt_bytes(engine=engine, session=session, target_table=target_table,
                                              target_geom=target_geom, srid=srid, pk_fields=pk_fields,
                                              safe_cols=safe_cols, layer_name=layer_name, x=x, y=y, z=z, extent=extent,
                                              buffer=buffer, resolution=resolution)
        else:
            return cls._get_fallback_mvt_bytes(model_cls=model_cls, session=session, target_geom=target_geom,
                                               requested_cols=safe_cols + pk_fields, layer_name=layer_name, x=x, y=y,
                                               z=z)

    @classmethod
    def _get_postgis_mvt_bytes(cls, engine, session: Session | None, target_table: str, target_geom: str, srid: int,
            pk_fields: list[str], safe_cols: list[str], layer_name: str, x: int, y: int, z: int, extent: int = 4096,
            buffer: int = 64, resolution: float = 0, ) -> bytes:
        """Executes native PostGIS ST_AsMVT pipeline directly inside PostgreSQL using spatial index filtering."""

        quoted_geom = f'"{target_geom}"'
        quoted_table = f'"{target_table}"'

        if pk_fields:
            pk_expr = " || '-' || ".join([f'"{f}"' for f in pk_fields]) + " AS pk"
        else:
            pk_expr = '"id" AS pk'

        cols_sql = ", " + ", ".join([f'"{c}"' for c in safe_cols]) if safe_cols else ""

        # Geometry transformation for rendering (3857)
        geom_transform = (f'ST_Transform({quoted_geom}, 3857)' if srid != 3857 else quoted_geom)

        if resolution > 0:
            geom_transform = f'ST_SimplifyPreserveTopology({geom_transform}, {resolution})'

        # Target bounding box filter expression (Uses native GIST index!)
        if srid != 3857:
            native_bbox_filter = f'{quoted_geom} && ST_Transform(tile_bounds.bbox, {srid})'
        else:
            native_bbox_filter = f'{quoted_geom} && tile_bounds.bbox'

        query = text(f"""
                WITH tile_bounds AS (
                    SELECT ST_TileEnvelope(:z, :x, :y) AS bbox
                ),
                geom_prep AS (
                    SELECT
                        {pk_expr},
                        {geom_transform} AS geom_3857
                        {cols_sql}
                    FROM {quoted_table}, tile_bounds
                    WHERE {quoted_geom} IS NOT NULL 
                      AND {native_bbox_filter}
                ),
                mvt_features AS (
                    SELECT
                        pk,
                        ST_AsMVTGeom(
                            geom_3857,
                            tile_bounds.bbox,
                            extent => :extent,
                            buffer => :buffer,
                            clip_geom => true
                        ) AS geom
                        {cols_sql}
                    FROM geom_prep, tile_bounds
                )
                SELECT ST_AsMVT(mvt_features.*, :layer_name, :extent, 'geom')
                FROM mvt_features
                WHERE geom IS NOT NULL;
            """)

        params = {"z": z, "x": x, "y": y, "extent": extent, "buffer": buffer, "layer_name": layer_name}

        if session:
            mvt_res = session.execute(query, params).scalar()
        else:
            with Session(engine) as s:
                mvt_res = s.execute(query, params).scalar()

        return bytes(mvt_res) if mvt_res else b""

    @classmethod
    def _get_fallback_mvt_bytes(cls, model_cls: type, session: Session | None, target_geom: str,
                                requested_cols: list[str], layer_name: str, x: int, y: int, z: int) -> bytes:
        """Fallback Python pipeline (SQLite/SpatiaLite) using ModelHelpersMixin.to_gdf()."""

        env_3857 = cls.xyz_to_extent_3857(x, y, z)
        minx, miny, maxx, maxy = env_3857['minx'].iloc[0], env_3857['miny'].iloc[0], env_3857['maxx'].iloc[0], \
            env_3857['maxy'].iloc[0]

        gdf = model_cls.to_gdf(geom_col_name=target_geom, target_crs=3857)

        if gdf.empty:
            return b""

        bbox_geom = box(minx, miny, maxx, maxy)
        gdf = gdf[gdf.geometry.intersects(bbox_geom)].copy()

        if gdf.empty:
            return b""

        keep_cols = [c for c in requested_cols if c in gdf.columns] + [gdf.geometry.name]
        gdf = gdf[keep_cols]

        return cls.gdf_to_mvt(gdf, (minx, miny, maxx, maxy), name=layer_name)


class MVTCache:
    DEFAULT_TTL = 60 * 60 * 24 * 7  # 7 Days

    # ---------------------------------------------------------------
    # 1. VARIANT BUILDER (Includes tile_size & resolution)
    # ---------------------------------------------------------------
    @staticmethod
    def build_variant(cols: list[str] | None = None, resolution: float = 0, tile_size: int = 4096,
            feature_count: int | None = None, extra: dict | None = None, ) -> str:
        """
        Builds a deterministic SHA-1 hash for tile variants.
        Includes tile_size to prevent extent mismatches between map renders.
        """
        normalized = {"cols": sorted(set(cols or [])), "resolution": resolution, "tile_size": tile_size, }

        if feature_count is not None:
            normalized["feature_count"] = feature_count

        if extra:
            clean_extra = {k: str(v) for k, v in extra.items()}
            normalized.update(clean_extra)

        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    # ---------------------------------------------------------------
    # 2. CACHE READ (Redis with SQLite Failover)
    # ---------------------------------------------------------------
    @classmethod
    def get_cached_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str) -> bytes | None:
        key = f"mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"

        # Try Redis First
        if redis_client:
            try:
                cached = redis_client.get(key)
                if cached:
                    return cached
            except (redis.ConnectionError, redis.TimeoutError) as err:
                da_logger.warning(f"[RedisUnavailable] Falling back to SQLite cache read: {err}")
            except Exception as err:
                da_logger.exception(f"[RedisReadError] {err}")

        # Fallback to SQLite
        return cls._get_sqlite_tile(layer_uuid, x, y, z, variant)

    # ---------------------------------------------------------------
    # 3. CACHE WRITE (Redis with SQLite Failover)
    # ---------------------------------------------------------------
    @classmethod
    def set_cached_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str, mvt_bytes: bytes,
            ttl: int = DEFAULT_TTL, ) -> None:
        if not mvt_bytes:
            return

        key = f"mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"

        redis_success = False
        if redis_client:
            try:
                redis_client.set(key, mvt_bytes, ex=ttl)
                redis_success = True
            except (redis.ConnectionError, redis.TimeoutError) as err:
                da_logger.warning(f"[RedisUnavailable] Write falling back to SQLite: {err}")
            except Exception as err:
                da_logger.exception(f"[RedisWriteError] {err}")

        if not redis_success:
            cls._set_sqlite_tile(layer_uuid, x, y, z, variant, mvt_bytes)

    # ---------------------------------------------------------------
    # 4. CONCURRENCY CONTROL (Distributed Tile Locking)
    # ---------------------------------------------------------------
    @classmethod
    def acquire_tile_lock(cls, layer_uuid: str, x: int, y: int, z: int, variant: str, timeout: int = 5) -> bool:
        if not redis_client:
            return True

        lock_key = f"lock:mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"
        try:
            return bool(redis_client.set(lock_key, b"1", nx=True, ex=timeout))
        except Exception:
            return True

    @classmethod
    def release_tile_lock(cls, layer_uuid: str, x: int, y: int, z: int, variant: str) -> None:
        if redis_client:
            lock_key = f"lock:mvt:{layer_uuid}:{variant}:{z}:{x}:{y}"
            try:
                redis_client.delete(lock_key)
            except Exception:
                pass

    # ---------------------------------------------------------------
    # 5. SQLITE FALLBACK IMPLEMENTATION
    # ---------------------------------------------------------------
    @classmethod
    def _get_sqlite_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str) -> bytes | None:
        try:
            engine = DBBase.get_engine(db_key="cache_db")
            with Session(engine) as session:
                table = cls._ensure_sqlite_table(session, layer_uuid)
                return session.execute(
                    text(f"SELECT mvt FROM {table} WHERE x=:x AND y=:y AND z=:z AND variant=:variant"),
                    {"x": x, "y": y, "z": z, "variant": variant}, ).scalar_one_or_none()
        except Exception as err:
            da_logger.exception(f"[SQLiteCacheReadError] {err}")
            return None

    @classmethod
    def _set_sqlite_tile(cls, layer_uuid: str, x: int, y: int, z: int, variant: str, mvt_bytes: bytes) -> None:
        try:
            engine = DBBase.get_engine(db_key="cache_db")
            with Session(engine) as session:
                table = cls._ensure_sqlite_table(session, layer_uuid)
                session.execute(text(f"""
                        INSERT INTO {table} (x, y, z, variant, mvt) 
                        VALUES (:x, :y, :z, :variant, :mvt)
                        ON CONFLICT (x, y, z, variant) 
                        DO UPDATE SET mvt = EXCLUDED.mvt
                    """), {"x": x, "y": y, "z": z, "variant": variant, "mvt": mvt_bytes}, )
                session.commit()
        except Exception as err:
            da_logger.exception(f"[SQLiteCacheWriteError] {err}")

    @staticmethod
    def _ensure_sqlite_table(session: Session, uuid: str) -> str:
        clean_uuid = "".join(c for c in uuid if c.isalnum() or c == "_")
        table_name = f'"{clean_uuid}_xyz"'

        session.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                z INTEGER NOT NULL,
                variant TEXT NOT NULL,
                mvt BLOB NOT NULL,
                PRIMARY KEY (x, y, z, variant)
            )
        """))
        session.commit()
        return table_name

    @classmethod
    def clear_layer_cache(cls, layer_uuid: str) -> int:
        cleared_count = 0
        if redis_client:
            try:
                keys = redis_client.keys(f"mvt:{layer_uuid}:*")
                if keys:
                    cleared_count = redis_client.delete(*keys)
            except Exception as err:
                da_logger.exception(f"[RedisCacheClearError] {err}")

        # Clear SQLite as well
        try:
            engine = DBBase.get_engine(db_key="cache_db")
            with Session(engine) as session:
                table = cls._ensure_sqlite_table(session, layer_uuid)
                res = session.execute(text(f"DELETE FROM {table}"))
                session.commit()
                cleared_count += res.rowcount or 0
        except Exception as err:
            da_logger.exception(f"[SQLiteCacheClearError] {err}")

        da_logger.info(f"Cleared {cleared_count} tiles for layer {layer_uuid}")
        return cleared_count
