import base64
import gzip
import io
import math
import re
import uuid
from typing import Optional, Union

import lxml.etree as ET
from digitalarzengine.utils.singletons import da_logger
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.damapql.dch.api_enum import DataModel, ModelType
from app.damapql.dch.models import LayerInfo, LayerAccessibility, DBConnection
from app.damapql.utils.adapters.db import DBBase
from app.damapql.utils.adapters.services.spatial_db import SpatialDB
from app.damapql.utils.adapters.services.db_query import DBQuery



class DCHUtils:
    @staticmethod
    def gzip_data(data: str) -> str:
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

    @classmethod
    def short_table_name(cls, filename_without_ext: str, max_len: int = 60, li_uuid: str = None) -> str:
        """
        Produce a short, Postgres-safe table name so index names stay < 63 chars.
        """
        base = cls._slug(filename_without_ext)
        base = f"geo_{base}"
        cut = max_len - (len(base) + 1)

        if li_uuid is None:
            li_uuid = cls.get_uuid1()[:cut]
        else:
            li_uuid = li_uuid[:cut]

        # base = base[:max(cut, 1)]
        return f"{base}_{li_uuid}"

    @staticmethod
    def create_title(name: str) -> str:
        title = name.title()
        for val in ["_"]:
            title = title.replace(val, " ")
        return title

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


    @classmethod
    def create_vector_layer_info(
            cls,
            engine: Engine,
            table_name: str,
            uuid: Optional[str] = None,
            layer_category_id=None,
            user=None,
            schema: Optional[str] = None,
    ) -> bool:
        """
        Create LayerAccessibility + LayerInfo for a vector table.
        Engine-only version. No GeoDBManager, no db-key string.
        """
        if uuid is None:
            uuid = cls.get_uuid1()

        try:
            spatial_db = SpatialDB(engine)

            # Use schema-aware table existence if your SpatialDB supports it
            if not spatial_db.is_table(table_name):
                da_logger.error(f"Table does not exist: {table_name}")
                return False

            table = spatial_db.get_table(table_name)

            geometry_cols = [
                col for col in list(table.columns)
                if "geometry" in str(col.type).lower()
            ]
            if not geometry_cols:
                da_logger.error(f"No geometry column found in table: {table_name}")
                return False

            geom_col = geometry_cols[0]

            # Build column dictionary here
            columns = {col.name: str(col.type) for col in table.columns}

            primary_cols = [key.name for key in inspect(table).primary_key]

            if len(primary_cols) == 0:
                quoted_table = cls._quote_table(schema, table_name)

                if "dch_id" not in table.columns:
                    stmt = f"ALTER TABLE {quoted_table} ADD COLUMN dch_id SERIAL PRIMARY KEY"
                    spatial_db.execute_ddl(stmt)
                else:
                    stmt = f"""
                    ALTER TABLE {quoted_table}
                    DROP CONSTRAINT IF EXISTS {table_name}_pkey;
                    ALTER TABLE {quoted_table}
                    ADD PRIMARY KEY (dch_id);
                    """
                    spatial_db.execute_ddl(stmt)

                table = spatial_db.get_table(table_name)
                columns = {col.name: str(col.type) for col in table.columns}
                primary_cols = ["dch_id"]

            elif "id" in table.columns:
                primary_cols = ["id"]

            da_logger.debug(f"{table_name}, primary_key, {primary_cols}")

            srid = getattr(geom_col.type, "srid", None)
            if srid in (-1, 0, None):
                srid = spatial_db.get_geom_srid(table_name, geom_col.name)

            if srid in (-1, 0, None):
                da_logger.error(f"Could not determine SRID for {table_name}.{geom_col.name}")
                return False

            quoted_table = cls._quote_table(schema, table_name)
            query = f"SELECT * FROM {quoted_table}"

            gdf = spatial_db.execute_query_as_gdf(
                query=query,
                geom_col=geom_col.name,
                srid=srid,
            )

            if gdf.empty:
                da_logger.error(f"Table {table_name} returned no rows.")
                return False

            layer_name = table_name[:table_name.rfind("_")] if uuid in table_name else table_name
            layer_name = layer_name.replace("tbl", "")

            db_config = DBBase.engine_to_db_config(engine)
            db_conn = DBConnection.create_from_dict(db_config)

            la = LayerAccessibility.create_db_accessibility(
                table_name=table_name if schema is None else f"{schema}.{table_name}",
                db_conn=db_conn,
                columns=columns,
                pk_cols=primary_cols
            )

            lf = LayerInfo()
            lf.uuid = uuid
            lf.title = cls.create_title(layer_name)
            lf.layer_name = layer_name
            lf.layer_access_id = la.id
            lf.category_id = int(layer_category_id) if layer_category_id is not None else None
            lf.quality_level_id = 5

            try:
                lf.srid = int(gdf.crs.to_epsg()) if gdf.crs and gdf.crs.to_epsg() else int(srid)
            except Exception:
                lf.srid = int(srid)

            try:
                lf.proj4 = gdf.crs.to_proj4() if gdf.crs else None
            except Exception:
                lf.proj4 = None

            lf.extent = gdf.total_bounds.tolist()

            try:
                lf.extent_3857 = gdf.to_crs(crs=3857).total_bounds.tolist()
            except Exception:
                lf.extent_3857 = None

            lf.data_model = DataModel.V.name
            lf.model_type = ModelType.Geom.name
            lf.geom_type = list(set(gdf.geom_type.tolist()))
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