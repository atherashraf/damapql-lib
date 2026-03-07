import inspect

from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date
from typing import Union, Any, Tuple, List, Optional
import geopandas as gpd
import pandas as pd
from geoalchemy2 import WKBElement, Geometry
from geoalchemy2.shape import to_shape
from shapely import wkb

from sqlalchemy import MetaData, Engine, Select, func, ARRAY
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Session, Query, InstrumentedAttribute

from digitalarzengine.io.managers.db_manager import DBString, DBParams, GeoDBManager, DBManager
from utils.singletons import da_logger

from settings import DA_APPS, DATABASES

metadata_obj = MetaData()


# Base = declarative_base(metadata=metadata_obj)




class DBBase(DeclarativeBase):
    metadata = metadata_obj

    class _Meta:
        is_spatial = True
        db_key = None
        app_label = None

    # def __init__(self, session:Session = None):
    #     super().__init__()
    #     # Initialize the session when the object is created
    #     # Assuming 'manager' is a method that returns an instance of a manager that handles session creation
    #     self.metadata = metadata_obj
    #     self._session: Session = self.manager().SessionLocal() if session is None else session
    #
    # def __del__(self):
    #     # Close the session when the object is destroyed
    #     # Assuming the session object has a proper 'close' method
    #     self._session.close()

    @classmethod
    def session(cls, session: Optional[Session] = None) -> Session:
        # return cls.manager().SessionLocal()
        return cls.manager().managed_session(session=session)

    @classmethod
    def get_primary_key_fields(cls) -> list[str]:
        return [col.name for col in cls.__mapper__.primary_key]

    @classmethod
    def get_geom_col_name_srid(cls) -> (str, str):
        geom_cols = cls.get_geom_cols()

        names = list(geom_cols.keys())
        if len(names) > 0:
            geom_col_name = names[0]
            geom_col_srid = geom_cols[geom_col_name].type.srid
            return geom_col_name, geom_col_srid

    @classmethod
    def get_geom_cols(cls) -> dict:
        """
        :return: dict of geom column name as key and its details as value
        """
        if cls._Meta.is_spatial:
            geom = {attribute: value for attribute, value in vars(cls).items() if
                    hasattr(value, "type") and "geometry" in str(value.type)}
            return geom
        return {}

    @staticmethod
    def obj_to_shapely(obj, geom_col='geom'):
        return wkb.loads(obj[geom_col].desc)

    @staticmethod
    def obj_to_envelop(obj, session: Session, in_latlon: bool = False, geom_col='geom'):
        # Transform the geometry to EPSG:4326
        geom = session.scalar(func.ST_Transform(getattr(obj, geom_col), 4326)) if in_latlon else getattr(obj, geom_col)

        # Get the envelope (bounding box) of the geometry
        envelope = session.scalar(func.ST_Envelope(geom))

        # Convert the envelope to a Shapely geometry
        envelope_geom = wkb.loads(envelope.desc)

        return envelope_geom

    @staticmethod
    def obj_to_xy(obj, session: Session, in_latlon: bool = False, geom_col='geom'):
        geom = session.scalar(func.ST_Transform(getattr(obj, geom_col), 4326)) if in_latlon else getattr(obj, geom_col)
        geom_type = session.scalar(func.GeometryType(geom))
        if geom_type in ['POLYGON', 'MULTIPOLYGON']:
            centroid = session.scalar(func.ST_Centroid(geom))
            x = session.scalar(func.ST_X(centroid))
            y = session.scalar(func.ST_Y(centroid))
        else:
            x = session.scalar(func.ST_X(geom))
            y = session.scalar(func.ST_Y(geom))

        return x, y

    @classmethod
    def to_geojson(cls):
        gdf = cls.to_dataframe(is_spatial=True)
        # boolean_columns = gdf.select_dtypes(include=[bool]).columns.tolist()
        # for column in boolean_columns:
        #     gdf[column] = gdf[column].map({True: 'True', False: 'False'})

        return gdf.__geo_interface__

    @classmethod
    def to_dataframe(cls, qs: Union[Query, Select] = None, is_spatial=True, geom_col=None) -> Union[
        pd.DataFrame, gpd.GeoDataFrame]:
        """
        Convert SQLAlchemy model instances to a DataFrame or GeoDataFrame.
        Uses a context manager to handle the session properly.

        :param qs: Optional, SQLAlchemy Query object. If None, default to querying the class.
        :param is_spatial: Boolean, indicates if the result should be a GeoDataFrame.
        :return: pd.DataFrame or gpd.GeoDataFrame
        """
        # Use a context manager to ensure the session is handled properly
        with cls.manager().managed_session() as session:
            if qs is None:
                qs = session.query(cls)
            elif isinstance(qs, Select):
                qs = session.execute(qs).all()
            try:
                # Fetch data within the session's scope
                data_dicts = [obj.__dict__ for obj in qs]
            except:
                # If the regular dictionary access fails, use a more direct approach
                data_dicts = [obj._asdict() for obj in qs]

        # Remove SQLAlchemy's internal tracking attribute
        data_dicts = [{k: v for k, v in d.items() if k != '_sa_instance_state'} for d in data_dicts]

        if is_spatial and cls._Meta.is_spatial:
            if geom_col is None:
                geom_col_name, geom_col_srid = cls.get_geom_col_name_srid()
            else:
                geom_col_srid = geom_col.type.srid
                geom_col_name = geom_col.name
            # Convert binary geometry data to geometric objects
            for row in data_dicts:
                geom = wkb.loads(bytes(row[geom_col_name].data), hex=True)
                if geom.is_valid:
                    row[geom_col_name] = geom
            # Create a GeoDataFrame if spatial
            if len(data_dicts) > 0:
                df = gpd.GeoDataFrame(data_dicts, geometry=geom_col_name, crs=f"EPSG:{geom_col_srid}")
            else:
                df = gpd.GeoDataFrame()
        else:
            # Create a regular DataFrame if not spatial
            df = pd.DataFrame(data_dicts)
            # Drop GeoAlchemy2 columns if is_spatial is False
            geom_columns = [col.name for col in inspect(cls).columns if isinstance(col.type, Geometry)]
            df_geom_columns = [col for col in geom_columns if col in df.columns.tolist()]
            if len(df_geom_columns) > 0:
                df.drop(columns=geom_columns, inplace=True)

        return df

    @classmethod
    def unary_union_to_gdf(cls, session: Session = None) -> gpd.GeoDataFrame:
        geom_cols = list(cls.get_geom_cols().values())
        if len(geom_cols) > 0:
            geom_col = geom_cols[0]
            srid = geom_col.type.srid
            stmt = func.ST_Union(geom_col)
            if session is not None:
                result = session.query(stmt).scalar()
            else:
                result = cls.manager().execute_query_as_one(stmt)
            shp = cls.convert_wkb_element_to_shapely(result)
            return gpd.GeoDataFrame(geometry=[shp], crs=f"EPSG:{srid}")
        return None

    # Function to convert a single SQLAlchemy object to GeoDataFrame
    @classmethod
    def obj_to_geodf(cls, obj, geom_col_name=None):
        obj_dict = {}
        srid = 0
        columns = []
        for attribute, value in vars(cls).items():
            if hasattr(value, 'type') and hasattr(value, 'impl'):
                columns.append(attribute)
                if geom_col_name is None and "geometry" in str(value.type):
                    srid = value.type.srid
                    geom_data = getattr(obj, attribute)
                    if geom_data is not None:
                        geom = wkb.loads(bytes(geom_data.data))
                        obj_dict['geometry'] = geom
                elif geom_col_name is not None and geom_col_name == attribute:
                    srid = value.type.srid
                    geom_data = getattr(obj, attribute)
                    if geom_data is not None:
                        geom = wkb.loads(bytes(geom_data.data))
                        obj_dict['geometry'] = geom
                else:
                    attr_value = getattr(obj, attribute)
                    if isinstance(attr_value, date):
                        obj_dict[attribute] = attr_value.isoformat()  # Convert date to ISO format string
                    else:
                        obj_dict[attribute] = attr_value

        if 'geometry' not in obj_dict:
            # raise ValueError("No geometry attribute found.")
            return pd.DataFrame([obj_dict], columns=columns)

        return gpd.GeoDataFrame([obj_dict], geometry='geometry', crs=srid)

    @classmethod
    def query(
            cls, *entities: [Any], session=None, **kwargs: Any
    ) -> Query[Any]:
        """
        Return a new :class:`_query.Query` object corresponding to this
        :class:`_orm.Session`.
        Exampla:
            LayerCategory.query().filter_by(id=3).first()
            LayerCategory.query(LayerCategory.main,LayerCategory.id).filter_by(id=3).first()
            LayerCategory.query().values(LayerCategory.main,LayerCategory.id)
        """

        with session if session is not None else cls.manager().managed_session() as session:  # Get a session for query construction
            if len(entities) == 0:
                query = session.query(cls)
            else:
                query = session.query(*entities)
            return query

    @classmethod
    def get_engine(cls) -> Engine:
        return cls.manager().get_engine()

    @classmethod
    def get_db_key(cls, app_label=None):
        if hasattr(cls._Meta, "db_key") and cls._Meta.db_key is not None:
            return cls._Meta.db_key
        if app_label is None:
            if hasattr(cls._Meta, "app_label") and cls._Meta.app_label is not None:
                app_label = getattr(cls._Meta, "app_label")
            else:
                app_label = cls.__module__.split(".")[-2]
        return DA_APPS[app_label]["db_key"]

    @classmethod
    def get_fields(cls, skip_fields=None):
        if skip_fields is None:
            skip_fields = []

        attributes = inspect.getmembers(cls, lambda a: not inspect.isroutine(a))
        col_names = [
            name for name, attr in attributes
            if hasattr(attr, "type") and name not in skip_fields
        ]
        return col_names

    # def to_object(self):
    #     pydantic_cls = getattr(self, '__pydantic__', None)
    #     if pydantic_cls is not None:
    #         return pydantic_cls.model_validate(self)
    #
    #     # Fallback: return dict version of the SQLAlchemy object
    #     print(f"Warning: {self.__class__.__name__} has no `__pydantic__` defined. Returning dict instead.")
    #
    #     return {
    #         key: value for key, value in self.__dict__.items()
    #         if not key.startswith("_")  # exclude SQLAlchemy internals like _sa_instance_state
    #     }
    def to_object(self):
        pydantic_cls = getattr(self, '__pydantic__', None)
        if pydantic_cls is not None:
            # Get columns and relationships
            data = {key: value for key, value in self.__dict__.items() if not key.startswith("_")}

            # Add additional relationships if present in the Pydantic model
            expected_fields = set(pydantic_cls.model_fields.keys())
            for rel_name in expected_fields - data.keys():
                if hasattr(self, rel_name):
                    data[rel_name] = getattr(self, rel_name)

            return pydantic_cls.model_validate(data)

        # Fallback to dict
        print(f"Warning: {self.__class__.__name__} has no `__pydantic__` defined. Returning dict instead.")
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}

    def save(self, session: Union[Session, AsyncSession] = None, app_label=None, unique_fields=[]) -> Any:
        """
             This method checks if the object exists based on unique fields or the primary key.
             If it exists, it updates the existing row; otherwise, it creates a new row.

             @param app_label: The app label for database context (optional).
             @param unique_fields: List of fields that should be unique for the object.
             @return: The primary key value of the saved object.
        """
        db_key = self.get_db_key(app_label)
        is_external_session = session is not None
        if db_key is not None:
            is_spatial = self._Meta.is_spatial
            manager = self.get_manager(db_key, is_spatial)
            with manager.managed_session(session=session) as session:
                existing_record = None

                if unique_fields:
                    filter_criteria = {
                        field: getattr(self, field)
                        for field in unique_fields
                        if hasattr(self.__class__, field)
                    }
                    existing_record = session.query(self.__class__).filter_by(**filter_criteria).first()
                    if existing_record:
                        da_logger.debug(f"Data already exists against {', '.join(unique_fields)} for {filter_criteria}")

                # If no unique fields, check by primary key
                if not existing_record:
                    pk_field = self.get_primary_key_fields()[0]
                    pk_value = getattr(self, pk_field, None)
                    if pk_value is not None:
                        existing_record = session.get(self.__class__, pk_value)

                if existing_record:
                    da_logger.debug('Updating existing record')
                    for attr, value in self.__dict__.items():
                        if attr in self.__mapper__.c.keys():
                            setattr(existing_record, attr, value)
                    # Inside your loop
                    # for attr, value in self.__dict__.items():
                    #     if attr in self.__mapper__.c.keys():
                    #         column = self.__mapper__.c[attr].type
                    #
                    #         # Handle lists of Enums (e.g., ARRAY(Enum))
                    #         if isinstance(column, ARRAY) and isinstance(column.item_type, SQLAlchemyEnum):
                    #             enum_class = column.item_type.enum_class
                    #             if isinstance(value, list):
                    #                 value = [v if isinstance(v, enum_class) else enum_class(v) for v in value]
                    #
                    #         setattr(existing_record, attr, value)

                    # session.add(existing_record)
                    local_object = existing_record
                    is_add = False
                else:
                    print('Creating new record')
                    # local_object = session.merge(self)
                    local_object = self
                    is_add = True
                if isinstance(session, AsyncSession):
                    self.save_async(local_object, session, is_add)
                else:
                    self.save_sync(local_object, session, is_add)
                # return local_object if is_external_session else local_object.to_object()
                return local_object if is_external_session else local_object.to_object()

        else:
            # print("Failed to get DB credentials")
            # traceback.print_exc()
            da_logger.exception("Failed to get DB credentials")
            return None

    @staticmethod
    async def save_async(obj, session: AsyncSession, is_add=True):
        if is_add:
            session.add(obj)
        await session.commit()
        await session.flush()

    @staticmethod
    def save_sync(obj, session: AsyncSession, is_add=True):
        if is_add:
            session.add(obj)
        session.commit()
        session.flush()
        session.refresh(obj)

    def delete(self, session: Union[Session, AsyncSession] = None, app_label=None):
        db_key = self.get_db_key(app_label)
        if db_key is not None:
            is_spatial = self._Meta.is_spatial
            manager = self.get_manager(db_key, is_spatial)
            with manager.managed_session(session) as session:
                # local_object = session.merge(self)
                local_object = self
                if isinstance(session, AsyncSession):
                    self.delete_async(local_object, session)
                else:
                    self.delete_sync(local_object, session)
        else:
            print("Failed to get DB credentials")

    @staticmethod
    def delete_sync(local_object, session: Session):
        session.delete(local_object)
        session.commit()
        session.flush()

    @staticmethod
    async def delete_async(self, local_object, session: AsyncSession):
        await session.delete(local_object)
        await session.commit()
        await session.flush()


    @staticmethod
    def get_db_params(db_config: Union[str, dict]):
        """
        :param db_config:
        :return:
        example of db_config
        "flood_forecast": {
            "engine": "postgresql+psycopg2",
            "host": "localhost",
            "name": "flood_forecast",
            "port": 5432,
            "user": "xxxxxxxxxx",
            "password": "xxxxxxxxx"
        },
        """
        if isinstance(db_config, str):
            db_config = DATABASES[db_config]
        # if isinstance(db_str, dict):
        if db_config['engine'] in ["sqlite"]:
            db_str = db_config["file_path"]
        else:
            db_str = DBString(**db_config)
        # engine='postgresql+psycopg2'
        params = DBParams(engine_name=db_config['engine'], con_str=db_str)
        return params

    @staticmethod
    def get_manager(db_info_key: Union[str, Engine] = None, is_spatial=False) -> Union[GeoDBManager, DBManager]:
        if db_info_key is None:
            raise ValueError("db_key is required or call manager function")

        db_params = DBBase.get_db_params(db_info_key) if not isinstance(db_info_key, Engine) else db_info_key
        if is_spatial:
            return GeoDBManager(db_params)
        else:
            return DBManager(db_params)

    @classmethod
    def manager(cls, engine: Engine = None) -> Union[DBManager, GeoDBManager]:
        is_spatial = cls._Meta.is_spatial
        if engine is None:
            db_key = cls.get_db_key()
            return cls.get_manager(db_key, is_spatial)
        else:
            return cls.get_manager(engine)

    @classmethod
    def extent_box_2_tuple(cls, box_string: str) -> Tuple[float, float, float, float]:
        values = box_string[4:-1].split(",")  # Remove 'BOX(' and ')' and split
        xmin, ymin = map(float, values[0].split())
        xmax, ymax = map(float, values[1].split())
        return (xmin, ymin, xmax, ymax)

    @staticmethod
    def convert_wkb_element_to_shapely(wkb_element: WKBElement):
        return wkb.loads(wkb_element.desc)

    @staticmethod
    def convert_wkb_element_to_gdf(wkb_element: WKBElement) -> gpd.GeoDataFrame:
        # Extract geometry from WKBElement
        geometry = wkb.loads(wkb_element.desc)

        # Extract SRID from WKBElement
        srid = wkb_element.srid

        # Create GeoDataFrame with the extracted geometry and SRID
        gdf = gpd.GeoDataFrame([{'geometry': geometry}], crs=f'EPSG:{srid}')

        return gdf

    @staticmethod
    def convert_to_sql_query(qs, engine) -> str:
        # Compile the query to a string with parameters
        compiled_qs = qs.compile(compile_kwargs={"literal_binds": True}, dialect=engine.dialect)
        query_string = str(compiled_qs)
        print(query_string)
        return query_string

    @classmethod
    def get_result_set_under_aoi(cls, aoi_gdf: gpd.GeoDataFrame, geom_col: InstrumentedAttribute) -> gpd.GeoDataFrame:
        # Assuming the AOI GeoDataFrame contains a single polygon
        srid = geom_col.type.srid
        geom_col_name = geom_col.name
        if aoi_gdf.crs.to_epsg() != srid:
            aoi_gdf = aoi_gdf.to_crs(epsg=srid)
        aoi_geom = aoi_gdf.unary_union

        manager = cls.manager()
        with manager.managed_session() as session:
            # Convert the AOI geometry to WKT (Well-Known Text)
            aoi_geom_wkt = aoi_geom.wkt

            # Query the database to get geometries within the AOI
            query = session.query(cls).filter(
                func.ST_Intersects(
                    getattr(cls, geom_col_name),
                    func.ST_GeomFromText(aoi_geom_wkt, srid)
                )
            )
            result_rows = []
            for row in query.all():
                row_dict = {column.name: getattr(row, column.name) for column in cls.__table__.columns}
                row_dict[geom_col_name] = to_shape(getattr(row, geom_col_name))  # Convert the geometry column
                result_rows.append(row_dict)
            # Create a GeoDataFrame from the list of dictionaries
            result_gdf = gpd.GeoDataFrame(result_rows, geometry=geom_col_name, crs=f'EPSG:{srid}')
            return result_gdf

    @classmethod
    def get_query_string(cls, qs):
        str = qs.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        # print(query.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        return str
