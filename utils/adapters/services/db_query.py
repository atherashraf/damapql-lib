from typing import Dict, Any, List, Optional, Union, Type

import pandas as pd
from sqlalchemy import text, Select, select, Table
from sqlalchemy.exc import SQLAlchemyError, InvalidRequestError
from sqlalchemy.orm import Session
import json


class DBQuery:

    @staticmethod
    def execute_stmt_as_df(
            self,
            stmt: Union[str, Select, Table],
            session: Optional[Session] = None,
            enum_columns: Optional[List[str]] = None
    ) -> pd.DataFrame:
        try:
            with self.managed_session(session) as session:
                if isinstance(stmt, Select):
                    rs = session.execute(stmt)
                elif isinstance(stmt, Table):
                    stmt = select(stmt)
                    rs = session.execute(stmt)
                else:  # raw SQL string
                    rs = session.execute(text(stmt))

                df = pd.DataFrame(rs.fetchall())
                if not df.empty:
                    df.columns = rs.keys()

                    if enum_columns:
                        for col in enum_columns:
                            if col in df.columns:
                                df[col] = df[col].apply(
                                    lambda x: [v.value if hasattr(v, 'value') else v for v in x] if isinstance(x, list)
                                    else x.value if hasattr(x, 'value') else x
                                )
                return df
        except SQLAlchemyError as e:
            print(f"Error executing statement: {e}")
            return pd.DataFrame()

    @staticmethod
    def execute_query_as_dict(
            session: Session,
            query: Union[str, Select],
            params: dict = None,
            ensure_unique: bool = True
    ) -> List[Dict[str, Any]]:
        try:
            if isinstance(query, str):
                rs = session.execute(text(query), params or {})
            else:  # assume it's a SQLAlchemy Select or similar
                rs = session.execute(query, params or {})
                if ensure_unique:
                    # Only apply unique() for ORM entity results; it's not applicable to
                    # plain column/mapping results (especially with JSON/JSONB values).
                    try:
                        is_orm_result = getattr(rs, "_attributes", {}).get("is_orm", False)
                        if is_orm_result:
                            rs = rs.unique()
                    except (TypeError, InvalidRequestError):
                        # Non-hashable row values or unsupported uniqueness; proceed without filtering.
                        pass

            rows = rs.mappings().all()
            return [dict(r) for r in rows]
        except Exception as e:
            # Optionally log or re-raise
            raise RuntimeError(f"Query execution failed: {e}") from e

    @staticmethod
    def execute_query_as_geojson(
            session: Session,
            stmt: Union[str, Select],
            geom_col: str = "geom",
            id_col: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute a SQLAlchemy statement or raw SQL and return a GeoJSON FeatureCollection.
        The query must include a geometry column (`geom_col`).
        """
        # Execute depending on type
        if isinstance(stmt, str):
            result = session.execute(text(stmt))
        else:  # SQLAlchemy Select or similar
            result = session.execute(stmt)

        features: List[Dict[str, Any]] = []
        for row in result.mappings():
            geom = row.get(geom_col)
            if geom is None:
                continue
            # ST_AsGeoJSON from SQL returns a JSON string, so parse it
            if isinstance(geom, str):
                geom = json.loads(geom)

            feature = {
                "type": "Feature",
                "geometry": geom,
                "properties": {k: v for k, v in row.items() if k != geom_col}
            }
            if id_col and id_col in row:
                feature["id"] = row[id_col]

            features.append(feature)

        return {"type": "FeatureCollection", "features": features}

    # NEW: return exactly one row (as a dict) or None
    @staticmethod
    def execute_query_as_one(
            session: Session,
            query: Union[str, Select],
            params: Optional[dict] = None,
            *,
            raise_if_none: bool = False,
            raise_if_multiple: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a query and return one row as a dict (or None).

        Args:
            session: SQLAlchemy Session.
            query: raw SQL string or SQLAlchemy Select.
            params: bound parameters for the query.
            raise_if_none: if True, raise ValueError when no row is found.
            raise_if_multiple: if True, raise ValueError when more than one row is found.

        Returns:
            A dict representing the single row, or None if no row (unless raise_if_none=True).
        """
        # Execute
        if isinstance(query, str):
            result = session.execute(text(query), params or {})
        else:
            result = session.execute(query, params or {})

        maps = result.mappings()

        if raise_if_multiple:
            # Materialize all to check multiplicity
            rows = maps.all()
            if not rows:
                if raise_if_none:
                    raise ValueError("No rows returned for query.")
                return None
            if len(rows) > 1:
                raise ValueError(f"Expected at most one row, got {len(rows)}.")
            return dict(rows[0])

        # Not enforcing uniqueness; just get first/one_or_none
        row = maps.one_or_none() if raise_if_none else maps.first()
        if row is None:
            if raise_if_none:
                raise ValueError("No rows returned for query.")
            return None
        return dict(row)

    @staticmethod
    def _infer_entity_from_select(query: Select) -> Optional[Type[Any]]:
        # Works for standard ORM selects: select(MyModel) or select(MyModel).options(...)
        try:
            desc = query.column_descriptions
            if not desc:
                return None
            return desc[0].get("entity")
        except Exception:
            return None

    @staticmethod
    def execute_query_as_pydantic(
            session: Session,
            query: Select,
            *,
            model: Optional[Type[Any]] = None,
            raise_if_none: bool = False,
            raise_if_multiple: bool = False
    ) -> Optional[Any]:
        """
        Execute an ORM Select and return a single row as its __pydantic__ model.
        Assumes the ORM model defines a __pydantic__ = <PydanticModelClass> attribute.

        - If `model` is not provided, it will be inferred from the Select.
        - If multiple rows are returned and `raise_if_multiple=True`, raises ValueError.
        - If no row is returned and `raise_if_none=True`, raises ValueError.
        """
        orm_model = model or DBQuery._infer_entity_from_select(query)
        if orm_model is None:
            raise ValueError("Could not infer ORM model from Select; pass `model=` explicitly.")

        pydantic_cls = getattr(orm_model, "__pydantic__", None)
        if pydantic_cls is None:
            raise ValueError(f"{orm_model.__name__} does not define __pydantic__.")

        result = session.execute(query)
        scalars = result.scalars()

        if raise_if_multiple:
            objs: List[Any] = scalars.all()
            if not objs:
                if raise_if_none:
                    raise ValueError("No rows returned for query.")
                return None
            if len(objs) > 1:
                raise ValueError(f"Expected at most one row, got {len(objs)}.")
            obj = objs[0]
        else:
            obj = scalars.one_or_none() if raise_if_none else scalars.first()

        if obj is None:
            if raise_if_none:
                raise ValueError("No rows returned for query.")
            return None

        # Pydantic v2: from_attributes=True pulls from SQLAlchemy attributes
        return pydantic_cls.model_validate(obj, from_attributes=True)

    @staticmethod
    def execute_query_all_as_pydantic(
            session: Session,
            query: Select,
            *,
            model: Optional[Type[Any]] = None
    ) -> List[Any]:
        """
        Execute an ORM Select and return ALL rows as a list of __pydantic__ instances.
        """
        orm_model = model or DBQuery._infer_entity_from_select(query)
        if orm_model is None:
            raise ValueError("Could not infer ORM model from Select; pass `model=` explicitly.")

        pydantic_cls = getattr(orm_model, "__pydantic__", None)
        if pydantic_cls is None:
            raise ValueError(f"{orm_model.__name__} does not define __pydantic__.")

        objs = session.execute(query).scalars().all()
        return [pydantic_cls.model_validate(o, from_attributes=True) for o in objs]
