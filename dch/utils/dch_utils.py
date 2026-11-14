import base64
import gzip
import io
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union, Dict, List, Any

import lxml.etree as ET
from digitalarzengine.utils.singletons import da_logger
from geopandas import GeoDataFrame
from pyproj import Transformer
from shapely.geometry import box
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine

from app.damapql.dch.api_enum import DataModel, ModelType
from app.damapql.dch.models import LayerInfo, LayerAccessibility, DBConnection
from app.damapql.settings import DEFAULT_EXTENT_SRID, DCH_APP_NAME
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.services.spatial_db import SpatialDB
from app.damapql.utils.adapters.services.db_query import DBQuery
from settings import DEFAULT_EXTENT, DEFAULT_SRID


class DCHUtils:

    @classmethod
    def get_spatial_tables_info(
            cls,
            engine: Engine,
            schema: Optional[str] = None,
    ) -> List[dict[str, Optional[Any]]]:
        """
        List spatial tables from a SQLAlchemy Engine along with their primary
        geometry column, concrete geometry type, and SRID.
        """
        inspector = inspect(engine)
        spatial_db = SpatialDB(engine)
        spatial_tables: List[dict[str, Optional[Any]]] = []

        with engine.connect() as conn:
            for tbl_name in inspector.get_table_names(schema=schema):
                try:
                    table = spatial_db.get_table(tbl_name, schema=schema)
                except TypeError:
                    table = spatial_db.get_table(tbl_name)

                if table is None:
                    continue

                geom_cols = [
                    col for col in table.columns
                    if "geometry" in str(col.type).lower()
                ]
                if not geom_cols:
                    continue

                geom_col = geom_cols[0]
                raw_geom_type = getattr(geom_col.type, "geometry_type", None)
                geom_type_upper = str(raw_geom_type).upper() if raw_geom_type else None

                raw_srid = getattr(geom_col.type, "srid", None)
                srid: Optional[int] = int(raw_srid) if raw_srid not in (-1, 0, None) else None

                needs_geom_sample = not geom_type_upper or geom_type_upper in (
                    "GEOMETRY",
                    "GEOMETRYCOLLECTION",
                )
                needs_srid_sample = srid is None

                if needs_geom_sample or needs_srid_sample:
                    qualified_tbl = cls._quote_table(schema, tbl_name)
                    sample_sql = text(
                        f'SELECT ST_GeometryType("{geom_col.name}") AS geom_type, '
                        f'ST_SRID("{geom_col.name}") AS srid '
                        f'FROM {qualified_tbl} '
                        f'WHERE "{geom_col.name}" IS NOT NULL LIMIT 1'
                    )
                    try:
                        row = conn.execute(sample_sql).first()
                        if row is not None:
                            if needs_geom_sample and row.geom_type:
                                geom_type_upper = str(row.geom_type).upper().removeprefix("ST_")
                            if needs_srid_sample and row.srid not in (-1, 0, None):
                                srid = int(row.srid)
                    except Exception:
                        pass

                spatial_tables.append({
                    "table_name": tbl_name,
                    "geom_col": geom_col.name,
                    "geom_type": geom_type_upper,
                    "srid": srid,
                })

        return spatial_tables

    @classmethod
    def get_spatial_table_names(
            cls,
            engine: Engine,
            schema: Optional[str] = None,
    ) -> List[str]:
        """List all table names containing geometry columns."""
        return [item["table_name"] for item in cls.get_spatial_tables_info(engine=engine, schema=schema)]

    @staticmethod
    def build_versioned_object_name(
            *,
            file_name: str,
            category_main: str,
            model_type: str,
            uuid_str: str,
            category_sub: str | None = None,
            ext: str | None = None,
            dt: datetime | None = None,
    ) -> str:
        """
        Builds an immutable, traceable, chronologically sortable MinIO object key:
          <category_main>[/<category_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>
        """
        now = dt or datetime.now(timezone.utc)
        ms = now.microsecond // 1000
        ts = f"{now.strftime('%Y%m%d_%H%M%S')}_{ms:03d}"

        raw_path = Path(file_name)
        resolved_ext = (ext or raw_path.suffix.lstrip(".")).lower()
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_path.stem).strip("_").lower() or "layer"

        def clean_part(val: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_-]+", "_", val.strip()).strip("/")

        parts = [clean_part(category_main)]
        if category_sub and category_sub.strip():
            parts.append(clean_part(category_sub))
        parts.append(clean_part(model_type))
        parts.append(f"{stem}_{clean_part(uuid_str)}_{ts}.{resolved_ext}")

        return "/".join(p for p in parts if p)

    @staticmethod
    def gzip_data(data: Union[str,Dict]) -> str:
        if isinstance(data, dict):
            data = json.dumps(data)
        compressed_data_buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed_data_buffer, mode="wb") as gzipf:
            gzipf.write(data.encode("utf-8"))
        return base64.b64encode(compressed_data_buffer.getvalue()).decode()

    @staticmethod
    def get_uuid1() -> str:
        return uuid.uuid1().hex

    @staticmethod
    def _slug(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    # utils.py or inside DCHUtils
    import re

    @staticmethod
    def get_app_prefix() -> str:
        prefix = DCH_APP_NAME if DCH_APP_NAME else "damapql"
        return prefix.strip().lower() + "_"

    @classmethod
    def format_table_name(cls, raw_name: str, max_len: int = 54) -> str:
        """
        Ensures table starts with prefix, contains valid characters,
        does not double-prefix, and fits within max_len (<= 54 chars).
        """
        prefix = cls.get_app_prefix()
        clean = cls._slug(raw_name).lower()

        # Prevent double prefixing
        if clean.startswith(prefix):
            clean = clean[len(prefix):]

        # Calculate max allowed length for the user portion
        allowed_user_len = max_len - len(prefix)
        truncated_user = clean[:max(allowed_user_len, 1)].rstrip("_")

        return f"{prefix}{truncated_user}"

    @classmethod
    def short_table_name(cls, filename_without_ext: str, max_len: int = 54, li_uuid: str = None) -> str:
        """
        Creates a guaranteed unique, Postgres-safe table name with prefix and UUID suffix.
        Total length <= 54 characters so idx_{table}_geom is <= 63 characters.
        """
        prefix = cls.get_app_prefix()

        if li_uuid is None:
            raw_uuid = cls.get_uuid1()
        else:
            raw_uuid = str(li_uuid)

        clean_uuid = raw_uuid.replace("-", "")
        uuid_suffix = clean_uuid[:12]

        slug_name = cls._slug(filename_without_ext).lower()
        if slug_name.startswith(prefix):
            slug_name = slug_name[len(prefix):]

        # max_len - prefix - '_' - uuid_suffix
        allowed_slug_len = max_len - len(prefix) - 1 - len(uuid_suffix)
        trimmed_slug = slug_name[:max(allowed_slug_len, 1)].rstrip("_")

        return f"{prefix}{trimmed_slug}_{uuid_suffix}"
    # @staticmethod
    # def create_title(name: str) -> str:
    #     title = name.title()
    #     for val in ["_"]:
    #         title = title.replace(val, " ")
    #     return title

    @staticmethod
    def create_title(name: str) -> str:
        """
        Converts snake_case or CamelCase to clean title.
        Preserves uppercase acronyms (WASA, NA, PP).
        """


        def fix(word: str) -> str:
            # keep acronyms (already uppercase)
            if word.isupper():
                return word

            # force 2-letter words uppercase (NA, PP, UC)
            if len(word) == 2:
                return word.upper()

            return word.capitalize()

        # Handle snake_case
        if "_" in name:
            parts = [p for p in name.split("_") if p]
            return " ".join(fix(p) for p in parts)

        # Handle CamelCase
        parts = re.findall(
            r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+',
            name
        )

        return " ".join(fix(p) for p in parts if p)

    @staticmethod
    def _resolve_engine(db: Union[str, Engine]) -> Engine:
        """
        Resolve SQLAlchemy engine from:
        - Engine instance
        - app_label / db key string
        """
        if isinstance(db, Engine):
            return db
        if isinstance(db, str):
            return DBBase.get_engine(app_label=db)
        raise TypeError("db must be a SQLAlchemy Engine or app_label/db key string")

    @staticmethod
    def _quote_table(schema: Optional[str], table_name: str) -> str:
        safe_table = DBQuery.safe_ident(table_name)
        if schema:
            safe_schema = DBQuery.safe_ident(schema)
            return f'{safe_schema}.{safe_table}'
        return safe_table

    @staticmethod
    def _get_user_id(user) -> Optional[int]:
        if isinstance(user, dict):
            return user.get("id")
        return getattr(user, "id", None)

    from typing import Optional
    from sqlalchemy import select
    from sqlalchemy.engine import Engine

    @classmethod
    def _resolve_db_connection(
            cls,
            engine: Engine,
            db_conn_name: Optional[str] = None,
    ) -> Optional["DBConnection"]:
        """
        Resolve DBConnection either from the provided engine or by name.

        Returns:
            DBConnection | None
        """
        try:
            if not db_conn_name:
                db_config = DBBase.engine_to_db_config(engine)
                if not db_config:
                    da_logger.error("Could not derive DB config from engine.")
                    return None

                db_conn = DBConnection.create_from_dict(db_config)
                if db_conn is None:
                    da_logger.error("DBConnection.create_from_dict returned None.")
                    return None

                return db_conn

            with DBConnection.managed_session() as db:
                stmt = select(DBConnection).where(DBConnection.name == db_conn_name)
                db_conn = db.execute(stmt).scalar_one_or_none()

                if db_conn is None:
                    da_logger.error(f"DBConnection not found for name: {db_conn_name}")
                    return None

                return db_conn

        except Exception as e:
            da_logger.exception(f"ERROR resolving DB connection: {str(e)}")
            return None

    @staticmethod
    def _transform_extent(extent, from_srid, to_srid):
        """
        Transform extent [minx, miny, maxx, maxy] from one SRID to another.
        Returns the original extent if transformation fails.
        """
        try:
            if not extent or len(extent) != 4:
                da_logger.warning(f"Invalid extent for transformation: {extent}")
                return extent

            if from_srid in (None, 0, -1):
                da_logger.warning(f"Invalid source SRID for extent transform: {from_srid}")
                return extent

            if to_srid in (None, 0, -1):
                da_logger.warning(f"Invalid target SRID for extent transform: {to_srid}")
                return extent

            if int(from_srid) == int(to_srid):
                return list(extent)

            geom = box(*extent)
            gdf = GeoDataFrame(geometry=[geom], crs=f"EPSG:{int(from_srid)}")
            gdf = gdf.to_crs(epsg=int(to_srid))
            return gdf.total_bounds.tolist()

        except Exception as e:
            da_logger.warning(
                f"Extent transform failed from EPSG:{from_srid} to EPSG:{to_srid}: {e}"
            )
            return list(extent) if extent else extent

    @classmethod
    def create_vector_layer_info(
            cls,
            engine: Engine,
            table_name: str,
            uuid: Optional[str] = None,
            layer_category_id=None,
            user=None,
            schema: Optional[str] = None,
            db_conn_name: Optional[str] = None,
            title: Optional[str] = None,
            model_type: Optional[str] = None,
            symbology: Optional[str] = None
    ) -> bool:
        """
        Create LayerAccessibility + LayerInfo for a vector table.

        Engine-only version.
        No GeoDBManager, no db-key string.
        """
        if uuid is None:
            uuid = cls.get_uuid1()

        try:
            spatial_db = SpatialDB(engine)

            # Prefer schema-aware table lookup if supported by SpatialDB
            try:
                table_exists = spatial_db.is_table(table_name, schema=schema)
            except TypeError:
                table_exists = spatial_db.is_table(table_name)

            if not table_exists:
                full_table_name = f"{schema}.{table_name}" if schema else table_name
                da_logger.error(f"Table does not exist: {full_table_name}")
                return False

            try:
                table = spatial_db.get_table(table_name, schema=schema)
            except TypeError:
                table = spatial_db.get_table(table_name)

            geometry_cols = [
                col for col in table.columns
                if "geometry" in str(col.type).lower()
            ]
            if not geometry_cols:
                da_logger.error(f"No geometry column found in table: {table_name}")
                return False

            geom_col = geometry_cols[0]

            columns = {col.name: str(col.type) for col in table.columns}
            primary_cols = [col.name for col in table.primary_key.columns]

            quoted_table = cls._quote_table(schema, table_name)

            if not primary_cols:
                if "dch_id" not in table.columns:
                    stmt = f'ALTER TABLE {quoted_table} ADD COLUMN "dch_id" SERIAL PRIMARY KEY'
                    spatial_db.execute_ddl(stmt)
                else:
                    stmt = f'ALTER TABLE {quoted_table} ADD PRIMARY KEY ("dch_id")'
                    spatial_db.execute_ddl(stmt)

                try:
                    table = spatial_db.get_table(table_name, schema=schema)
                except TypeError:
                    table = spatial_db.get_table(table_name)

                columns = {col.name: str(col.type) for col in table.columns}
                primary_cols = [col.name for col in table.primary_key.columns] or ["dch_id"]

            da_logger.debug(f"{table_name}, primary_key, {primary_cols}")

            srid = getattr(geom_col.type, "srid", None)
            if srid in (-1, 0, None):
                try:
                    srid = spatial_db.get_geom_srid(table_name, geom_col.name, schema=schema)
                except TypeError:
                    srid = spatial_db.get_geom_srid(table_name, geom_col.name)

            if srid in (-1, 0, None):
                da_logger.warning(
                    f"Could not determine SRID for {table_name}.{geom_col.name}. "
                    f"Using DEFAULT_SRID={DEFAULT_SRID}"
                )
                srid = DEFAULT_SRID

            query = f"SELECT * FROM {quoted_table}"
            gdf = spatial_db.execute_query_as_gdf(
                query=query,
                geom_col=geom_col.name,
                srid=srid,
            )

            # Build layer name safely
            if uuid in table_name and "_" in table_name:
                layer_name = table_name.rsplit("_", 1)[0]
            else:
                layer_name = table_name

            if layer_name.startswith("tbl"):
                layer_name = layer_name[3:]

            db_conn = cls._resolve_db_connection(engine=engine, db_conn_name=db_conn_name)
            if db_conn is None:
                da_logger.error(
                    f"Failed to resolve DB connection for table: "
                    f"{schema + '.' if schema else ''}{table_name}"
                )
                return False

            la = LayerAccessibility.create_db_accessibility(
                table_name=table_name if schema is None else f"{schema}.{table_name}",
                db_conn=db_conn,
                columns=columns,
                pk_cols=primary_cols,
            )

            if la is None:
                da_logger.error(f"Failed to create LayerAccessibility for table: {table_name}")
                return False



            resolved_title = (title or "").strip() or cls.create_title(layer_name)
            resolved_model_type = (
                model_type
                if (model_type and hasattr(ModelType, model_type))
                else ModelType.Geom.name
            )

            lf = LayerInfo()
            lf.uuid = uuid
            lf.title = resolved_title
            lf.layer_name = layer_name
            lf.layer_access_id = la.id
            lf.category_id = int(layer_category_id) if layer_category_id is not None else None
            lf.quality_level_id = 5
            lf.format = "POSTGIS"

            if symbology:
                try:
                    lf.layer_styling = (
                        json.loads(symbology) if isinstance(symbology, str) else symbology
                    )
                except Exception as style_err:
                    da_logger.warning(f"Invalid symbology JSON for {table_name}: {style_err}")

            if gdf.empty:
                da_logger.warning(
                    f"Table {table_name} returned no rows. "
                    f"Using DEFAULT_EXTENT transformed to SRID={srid}"
                )

                lf.srid = int(srid or DEFAULT_SRID)
                lf.proj4 = None

                lf.extent = cls._transform_extent(
                    DEFAULT_EXTENT,
                    DEFAULT_SRID,
                    lf.srid
                )

                lf.extent_3857 = cls._transform_extent(
                    DEFAULT_EXTENT,
                    DEFAULT_SRID,
                    3857
                )

                lf.geom_type = []

            else:
                try:
                    lf.srid = int(gdf.crs.to_epsg()) if gdf.crs and gdf.crs.to_epsg() else int(srid)
                except Exception:
                    lf.srid = int(srid or DEFAULT_SRID)

                try:
                    lf.proj4 = gdf.crs.to_proj4() if gdf.crs else None
                except Exception:
                    lf.proj4 = None

                try:
                    lf.extent = gdf.total_bounds.tolist()
                except Exception:
                    lf.extent = cls._transform_extent(
                        DEFAULT_EXTENT,
                        DEFAULT_SRID,
                        lf.srid
                    )

                try:
                    lf.extent_3857 = gdf.to_crs(crs=3857).total_bounds.tolist()
                except Exception:
                    lf.extent_3857 = cls._transform_extent(
                        DEFAULT_EXTENT,
                        DEFAULT_SRID,
                        3857
                    )

                try:
                    lf.geom_type = list(set(gdf.geom_type.tolist()))
                except Exception:
                    lf.geom_type = []

            lf.data_model = DataModel.V.name
            lf.model_type = resolved_model_type
            lf.uploaded_by = cls._get_user_id(user)

            lf.save()
            return True

        except Exception as e:
            da_logger.exception(f"ERROR in create_vector_layer_info: {str(e)}")
            return False


    @staticmethod
    def raster_symbolizer_parser(sld: str, summary_data, is_float: bool):
        parser = ET.XMLParser(encoding="utf-8")
        sld_tree = ET.XML(sld.encode("ascii"), parser=parser)
        tree = ET.ElementTree(sld_tree)
        root_elem = tree.getroot()
        sld_ns = root_elem.nsmap["sld"]
        raster_symbolizer = tree.findall(f".//{{{sld_ns}}}RasterSymbolizer")

        palette = []
        labels = []
        values = []

        for sym in raster_symbolizer:
            color_map_elem = sym.find(f"./{{{sld_ns}}}ColorMap")
            if color_map_elem is None:
                continue

            for child in color_map_elem:
                try:
                    q = child.attrib["quantity"]
                    c = child.attrib["color"]
                    l = child.attrib.get("label", c)
                    o = float(child.attrib.get("opacity", "1"))
                    alpha = f"{round(o * 255):02X}"
                    rgba_hex = c.upper() + alpha

                    values.append(q)
                    palette.append(rgba_hex)

                    if str(c) != str(l):
                        labels.append(l)
                except Exception:
                    da_logger.error("error in sld " + str(child), exc_info=True)

        if is_float:
            sorted_items = sorted(zip(values, palette), key=lambda x: float(x[0]))
            values, palette = zip(*sorted_items)
            values = [float(v) for v in values]
            res = {
                "values": values,
                "palette": list(palette),
                "min_val": summary_data["min"],
                "max_val": summary_data["max"],
            }
            if math.isinf(res["values"][-1]):
                res["values"][-1] = (
                    res["max_val"]
                    if res["max_val"] > res["values"][-2]
                    else res["values"][-2] + 10
                )
        else:
            res = {
                "palette": {
                    values[i]: palette[i] for i in range(len(values))
                }
            }

        if labels:
            res["labels"] = labels

        return res

    @staticmethod
    def get_default_extents(target_srid: int = 3857) -> tuple[list[float], list[float]]:
        """
        Returns (native_extent, extent_3857) derived from DEFAULT_EXTENT in settings.
        Falls back to Pakistan extent in EPSG:3857 if not configured.
        """
        # Pakistan EPSG:3857 fallback [minx, miny, maxx, maxy]
        fallback_3857 = [6770000.0, 2690000.0, 8730000.0, 4440000.0]

        extent = DEFAULT_EXTENT
        # Parse if still arriving as a string from settings or env
        if isinstance(extent, str):
            try:
                if extent.strip().startswith("["):
                    extent = json.loads(extent)
                else:
                    extent = [float(x.strip()) for x in extent.split(",") if x.strip()]
            except Exception:
                extent = None

        if not extent or len(extent) != 4:
            extent = fallback_3857
            base_srid = 3857
        else:
            try:
                extent = [float(v) for v in extent]
                base_srid = int(DEFAULT_EXTENT_SRID or 3857)
            except (ValueError, TypeError):
                extent = fallback_3857
                base_srid = 3857

        target_srid = int(target_srid or 3857)

        # 1. Calculate EPSG:3857 bounds
        if base_srid == 3857:
            ext_3857 = [round(v, 4) for v in extent]
        else:
            try:
                tf_3857 = Transformer.from_crs(f"EPSG:{base_srid}", "EPSG:3857", always_xy=True)
                x1, y1 = tf_3857.transform(extent[0], extent[1])
                x2, y2 = tf_3857.transform(extent[2], extent[3])
                ext_3857 = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
            except Exception as e:
                da_logger.warning(f"Failed to project default extent to 3857: {e}")
                ext_3857 = fallback_3857

        # 2. Calculate target native layer SRID bounds
        if target_srid == base_srid:
            native_ext = [round(v, 4) for v in extent]
        elif target_srid == 3857:
            native_ext = list(ext_3857)
        else:
            try:
                tf_native = Transformer.from_crs(f"EPSG:{base_srid}", f"EPSG:{target_srid}", always_xy=True)
                nx1, ny1 = tf_native.transform(extent[0], extent[1])
                nx2, ny2 = tf_native.transform(extent[2], extent[3])
                native_ext = [round(nx1, 4), round(ny1, 4), round(nx2, 4), round(ny2, 4)]
            except Exception as e:
                da_logger.warning(f"Failed to project default extent to EPSG:{target_srid}: {e}")
                native_ext = list(ext_3857)

        return native_ext, ext_3857

