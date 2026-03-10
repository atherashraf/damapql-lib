import geopandas as gpd
from sqlalchemy import inspect, text, Table, MetaData
from sqlalchemy.engine import Engine


class SpatialDB:
    def __init__(self, engine: Engine):
        self.engine = engine

    def is_table(self, table_name: str):
        return inspect(self.engine).has_table(table_name)

    def get_table(self, table_name: str):
        meta = MetaData()
        return Table(table_name, meta, autoload_with=self.engine)

    def execute_ddl(self, sql: str):
        with self.engine.begin() as conn:
            conn.execute(text(sql))

    def execute_query_as_gdf(self, query: str, geom_col: str, srid: int):
        return gpd.read_postgis(
            query,
            self.engine,
            geom_col=geom_col,
            crs=f"EPSG:{srid}"
        )

    def get_geom_srid(self, table_name: str, geom_col: str):
        sql = """
        SELECT Find_SRID('', :table, :geom)
        """
        with self.engine.connect() as conn:
            res = conn.execute(
                text(sql),
                {"table": table_name, "geom": geom_col}
            ).scalar()

        return res