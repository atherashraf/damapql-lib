import base64
import gzip
import io
import math
import re
import uuid

import lxml.etree as ET
from typing import Union

from digitalarzengine.io.managers.db_manager import GeoDBManager
from sqlalchemy import inspect

from app.dch.api_enum import DataModel, ModelType
from app.dch.models import LayerInfo, LayerCategory, LayerQualityLevel, LayerAccessibility, DBConnection
from utils.adapters.db import DBBase


from utils.singletons import da_logger


class DCHUtils:
    @staticmethod
    def gzip_data(data):
        compressed_data_buffer = io.BytesIO()

        # Create a GZip file and add content (in this case, a text file)
        with gzip.GzipFile(fileobj=compressed_data_buffer, mode="wb") as gzipf:
            gzipf.write(data.encode("utf-8"))

        # Encode the GZip-compressed data in base64
        encoded_data = base64.b64encode(compressed_data_buffer.getvalue()).decode()
        return encoded_data

    @staticmethod
    def get_uuid1():
        return uuid.uuid1().hex

    @staticmethod
    def _slug(s: str) -> str:
        s = s.lower()
        s = re.sub(r'[^a-z0-9_]+', '_', s)  # keep letters/digits/underscores
        s = re.sub(r'_+', '_', s).strip('_')
        return s

    @classmethod
    def short_table_name(cls, filename_without_ext: str, max_len: int = 60) -> str:
        """
        Produce a short, Postgres-safe table name so that index names stay < 63 chars.
        """
        base = cls._slug(filename_without_ext)
        # short random id (8 hex) — enough for uniqueness without making names long
        sid = uuid.uuid4().hex[:8]
        # leave room for "_<sid>"
        cut = max_len - (len(sid) + 1)
        base = base[:max(cut, 1)]
        return f"{base}_{sid}"

    @staticmethod
    def create_title(name: str):
        title = name.title()
        for val in ["_"]:
            title = title.replace(val, " ")
        return title

    @classmethod
    def create_vector_layer_info(cls, db: Union[str, GeoDBManager], table_name, uuid=None,
                                 layer_category_id=None, user=None) -> bool:
        if uuid is None:
            uuid = cls.get_uuid1()

        manager: GeoDBManager = db if isinstance(db, GeoDBManager) else DBBase.get_manager(db, True)
        table = manager.get_sqlalchemy_table(table_name)
        is_table = manager.is_table(table_name)
        # basin_table = manager.get_sqlalchemy_table('pak_basins')
        # is_geometry_exist = [True for col in list(table.columns) if 'geometry' in str(col.type)]
        # crypto = CryptoUtils()
        conn = manager.con_2_dict()
        # password = crypto.decrypt_txt(conn["password"])
        # conn["password"] = crypto.encrypt_txt(conn["password"])
        conn_name = DBConnection.get_connection_name(conn["db_name"], conn["host"])
        db_conn = DBConnection.get_db_connection(conn_name, **conn)

        geometry_col = [col for col in list(table.columns) if 'geometry' in str(col.type)]
        primary_cols = []
        if len(geometry_col) > 0:
            try:
                #primary_cols = [key.name for key in inspect(table).primary_key]

                primary_cols = [key.name for key in inspect(table).primary_key]

                if len(primary_cols) == 0 and is_table:
                    if "dch_id" not in table.columns:
                        stmt = f'ALTER TABLE {table_name} ADD COLUMN dch_id SERIAL PRIMARY KEY'
                        manager.execute_ddl(stmt)
                    else:
                        # dch_id exists but is not PK — make it PK
                        stmt = f"""
                            ALTER TABLE {table_name}
                            DROP CONSTRAINT IF EXISTS {table_name}_pkey;
                            ALTER TABLE {table_name}
                            ADD PRIMARY KEY (dch_id);
                        """
                        manager.execute_ddl(stmt)

                    table = manager.get_sqlalchemy_table(table_name)  # Refresh table schema
                    primary_cols = ['dch_id']
                else:
                    if 'id' in table.columns:
                        primary_cols = ['id']

                da_logger.debug(f"{table_name}, primary_key, {primary_cols}")

                da_logger.debug(f"{table_name}, primary_key, {primary_cols}")
                if len(primary_cols) > 0:
                    query = f"Select * from {table_name}"
                    srid = geometry_col[0].type.srid
                    if srid == -1:
                        tbl = manager.get_sqlalchemy_table(table_name)
                        srid = manager.get_geom_col_srid(tbl, geometry_col[0].name)
                    # gdf = gpd.read_postgis(query, con=manager.get_engine(), geom_col=geometry_col[0].name, crs=srid)
                    gdf = manager.execute_query_as_gdf(query, srid, geom_col=geometry_col[0].name)
                    prefix = ""
                    postfix = ""
                    layer_name = table_name[:table_name.rfind("_")] if uuid in table_name else table_name
                    layer_name = f'{prefix}{layer_name.replace("tbl", "")}{postfix}'
                    # laLayerCategory.query().filter_by(id=layer_category_id).first()
                    la = LayerAccessibility.create_db_accessibility(
                        table_name, db_conn, gdf.dtypes.to_dict(), pk_cols=primary_cols
                    )

                    # layer_category = LayerCategory.query().filter_by(id=layer_category_id).first() if layer_category_id is not None else None
                    # remove this line (not needed)
                    # layer_category = LayerCategory.query().filter_by(id=layer_category_id).first() if layer_category_id is not None else None

                    lf = LayerInfo()
                    lf.uuid = uuid
                    lf.title = cls.create_title(layer_name)
                    lf.layer_name = layer_name

                    # IDs only — avoid cross-session objects
                    lf.layer_access_id = la.id
                    lf.category_id = int(layer_category_id) if layer_category_id is not None else None
                    lf.quality_level_id = 5

                    # keep the rest as-is
                    lf.srid = gdf.crs.srs and int(gdf.crs.srs.split(":")[-1])
                    lf.proj4 = gdf.crs.to_proj4()
                    lf.extent = gdf.total_bounds.tolist()
                    lf.extent_3857 = gdf.to_crs(crs=3857).total_bounds.tolist()
                    lf.data_model = DataModel.V.name
                    lf.model_type = ModelType.Geom.name
                    lf.geom_type = list(set(gdf.geom_type.tolist()))
                    lf.uploaded_by = user["id"] if isinstance(user, dict) and "id" in user else None

                    lf.save()

                    return True
            except Exception as e:
                da_logger.exception(f"ERROR: {str(e)}")

        return False

    @staticmethod
    def raster_symbolizer_parser(sld: str, summary_data, is_float):
        # tree = etree.parse(sld_fp)
        parser = ET.XMLParser(encoding="utf-8")
        sld_tree = ET.XML(sld.encode('ascii'), parser=parser)
        # sld_tree = ET.fromstring(sld, parser=parser)
        tree = ET.ElementTree(sld_tree)
        root_elem = tree.getroot()
        sld = root_elem.nsmap["sld"]
        raster_symbolizer = tree.findall(".//{%s}RasterSymbolizer" % sld)
        palette = []
        labels = []
        values = []
        for sym in raster_symbolizer:
            color_map_elem = sym.find("./{%s}ColorMap" % sld)
            for child in color_map_elem:
                try:
                    q = child.attrib['quantity']
                    c = child.attrib['color']
                    l = child.attrib.get('label', c)
                    o = float(child.attrib.get('opacity', '1'))  # Default is 1.0
                    alpha = f"{round(o * 255):02X}"
                    rgba_hex = c.upper() + alpha  # Convert to #RRGGBBAA
                    values.append(q)
                    palette.append(rgba_hex)
                    if str(c) != str(l):
                        labels.append(l)
                except Exception as e:
                    da_logger.error("error in sld " + str(child))
        if is_float:
            # Sort by quantity
            sorted_items = sorted(zip(values, palette), key=lambda x: float(x[0]))
            values, palette = zip(*sorted_items)
            values = [float(v) for v in values]
            res = {
                "values": values,
                "palette": list(palette),
                "min_val": summary_data["min"],
                "max_val": summary_data["max"]
            }
            if math.isinf(res['values'][-1]):
                res['values'][-1] = res["max_val"] if res["max_val"] > res["values"][-2] else res["values"][-2] + 10
        else:
            res = {
                "palette": {
                    values[i]: palette[i] for i in range(len(values))
                }
            }

        if labels:
            res["labels"] = labels

        return res
