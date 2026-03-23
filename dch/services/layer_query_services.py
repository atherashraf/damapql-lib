from __future__ import annotations

from typing import Any, Literal

import pandas as pd
from fastapi import HTTPException, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.damapql.dch.api_enum import AccessibilityType, DataModel
from app.damapql.dch.models import LayerInfo, LayerCategory, DBQuery

# Supported lookup modes for resolving a layer.
LayerLookupBy = Literal["uuid", "layer_name", "table_name"]


class LayerQueryService:
    """
    Service class for layer metadata and attribute querying.

    Notes
    -----
    - `identifier` is the lookup value.
    - `by` decides whether identifier is:
        - uuid
        - layer_name
        - table_name
    - `data_model` and `model_type` are columns in LayerInfo,
      so they must NOT be eager loaded with selectinload().
    """

    @staticmethod
    def get_layer_info(identifier: str, by: LayerLookupBy = "uuid") -> LayerInfo:
        """
        Fetch a single layer by uuid, layer_name, or table_name.
        """
        with LayerInfo.managed_session() as session:
            stmt = (
                select(LayerInfo)
                .options(
                    # Only real relationships should be eager-loaded.
                    selectinload(LayerInfo.layer_accessibility),
                    selectinload(LayerInfo.layer_category),
                    selectinload(LayerInfo.quality_level),
                )
            )

            if by == "uuid":
                stmt = stmt.where(LayerInfo.uuid == identifier)

            elif by == "layer_name":
                stmt = stmt.where(LayerInfo.layer_name == identifier)

            elif by == "table_name":
                stmt = stmt.where(
                    LayerInfo.layer_accessibility.has(table_name=identifier)
                )

            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported lookup type: {by}",
                )

            info = session.execute(stmt).scalar_one_or_none()

        if not info:
            raise HTTPException(
                status_code=404,
                detail=f"Layer not found: {identifier}",
            )

        return info

    @staticmethod
    def get_categories() -> list[dict[str, Any]]:
        """
        Return all layer categories.
        """
        with LayerCategory.managed_session() as session:
            stmt = select(LayerCategory).order_by(LayerCategory.main, LayerCategory.sub)
            categories = session.execute(stmt).scalars().all()

        return [
            {
                "id": obj.id,
                "main": obj.main,
                "sub": obj.sub,
                "name": f"{obj.main} - {obj.sub}" if obj.sub else obj.main,
            }
            for obj in categories
        ]

    @staticmethod
    def get_layers(
        app_label: str | None = None,
        category_id: int | None = None,
        data_model: str | None = None,
        model_type: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return filtered layer list for UI or API consumption.
        """
        with LayerInfo.managed_session() as session:
            stmt = (
                select(LayerInfo)
                .options(
                    selectinload(LayerInfo.layer_accessibility),
                    selectinload(LayerInfo.layer_category),
                    selectinload(LayerInfo.quality_level),
                )
                .order_by(LayerInfo.title)
            )

            if app_label:
                stmt = stmt.where(LayerInfo.app_label == app_label)

            if category_id:
                stmt = stmt.where(LayerInfo.category_id == category_id)

            layers = session.execute(stmt).scalars().all()

        payload: list[dict[str, Any]] = []

        for obj in layers:
            # These are ChoiceType-backed columns, not relationships.
            if data_model and (not obj.data_model or obj.data_model.code != data_model):
                continue

            if model_type and (not obj.model_type or obj.model_type.code != model_type):
                continue

            if title and title.lower() not in (obj.title or "").lower():
                continue

            payload.append(
                {
                    "id": obj.id,
                    "uuid": obj.uuid,
                    "title": obj.title,
                    "layer_name": obj.layer_name,
                    "app_label": obj.app_label,
                    "data_model": obj.data_model.code if obj.data_model else None,
                    "model_type": obj.model_type.code if obj.model_type else None,
                    "category": {
                        "id": obj.layer_category.id,
                        "main": obj.layer_category.main,
                        "sub": obj.layer_category.sub,
                    }
                    if obj.layer_category
                    else None,
                    "srid": obj.srid,
                    "geom_type": obj.geom_type,
                    "format": obj.format,
                    "zoom_range": obj.zoom_range,
                    "has_geometry": bool(
                        obj.layer_accessibility and obj.layer_accessibility.geometry_col
                    ),
                }
            )

        return payload

    @staticmethod
    def get_layer_tree(app_label: str | None = None) -> list[dict[str, Any]]:
        """
        Build a nested tree structure:
        main category -> sub category -> layer list
        """
        with LayerInfo.managed_session() as session:
            stmt = (
                select(LayerInfo)
                .options(
                    selectinload(LayerInfo.layer_category),
                    selectinload(LayerInfo.layer_accessibility),
                )
                .order_by(LayerInfo.title)
            )

            if app_label:
                stmt = stmt.where(LayerInfo.app_label == app_label)

            layers = session.execute(stmt).scalars().all()

        tree: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for obj in layers:
            main = obj.layer_category.main if obj.layer_category else "Uncategorized"
            sub = (
                obj.layer_category.sub
                if obj.layer_category and obj.layer_category.sub
                else "General"
            )

            tree.setdefault(main, {})
            tree[main].setdefault(sub, [])

            tree[main][sub].append(
                {
                    "id": obj.id,
                    "uuid": obj.uuid,
                    "title": obj.title,
                    "layer_name": obj.layer_name,
                    "data_model": obj.data_model.code if obj.data_model else None,
                    "model_type": obj.model_type.code if obj.model_type else None,
                    "has_geometry": bool(
                        obj.layer_accessibility and obj.layer_accessibility.geometry_col
                    ),
                }
            )

        return [
            {
                "name": main,
                "children": [
                    {
                        "name": sub,
                        "children": items,
                    }
                    for sub, items in subs.items()
                ],
            }
            for main, subs in tree.items()
        ]

    @staticmethod
    def get_layer_meta(
        identifier: str,
        by: LayerLookupBy = "uuid",
    ) -> dict[str, Any]:
        """
        Return detailed metadata for a single layer.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        access = info.layer_accessibility

        return {
            "id": info.id,
            "uuid": info.uuid,
            "title": info.title,
            "layer_name": info.layer_name,
            "app_label": info.app_label,
            "data_model": info.data_model.code if info.data_model else None,
            "model_type": info.model_type.code if info.model_type else None,
            "srid": info.srid,
            "proj4": info.proj4,
            "extent": info.extent,
            "extent_3857": info.extent_3857,
            "geom_type": info.geom_type,
            "format": info.format,
            "zoom_range": info.zoom_range,
            "layer_styling": info.layer_styling,
            "layer_setting": info.layer_setting,
            "temporal_res": info.temporal_res,
            "extra_metadata": info.extra_metadata,
            "category": {
                "id": info.layer_category.id,
                "main": info.layer_category.main,
                "sub": info.layer_category.sub,
            }
            if info.layer_category
            else None,
            "quality_level": {
                "id": info.quality_level.id,
                "level": info.quality_level.level,
                "name": info.quality_level.name,
                "description": info.quality_level.description,
            }
            if info.quality_level
            else None,
            "accessibility": {
                "id": access.id,
                "type": access.accessibility_type.code
                if access and access.accessibility_type
                else None,
                "table_name": access.table_name if access else None,
                "geometry_col": access.geometry_col if access else None,
                "pk_cols": access.pk_cols if access else [],
                "cols_name": access.cols_name if access else [],
                "cols_type": access.cols_type if access else [],
            }
            if access
            else None,
        }

    @staticmethod
    def ensure_vector_db_layer(info: LayerInfo) -> None:
        """
        Validate that the layer is a DB-backed vector layer.
        """
        if not info.data_model or info.data_model.code != DataModel.V.name:
            raise HTTPException(status_code=400, detail="Layer is not a vector layer")

        access = info.layer_accessibility
        if not access or not access.accessibility_type:
            raise HTTPException(
                status_code=400,
                detail="Layer accessibility is missing",
            )

        if access.accessibility_type.code != AccessibilityType.DB.name:
            raise HTTPException(status_code=400, detail="Layer is not DB-backed")

    @staticmethod
    def ensure_raster_layer(info: LayerInfo) -> None:
        """
        Validate that the layer is a raster layer.
        """
        if not info.data_model or info.data_model.code != DataModel.R.name:
            raise HTTPException(status_code=400, detail="Layer is not a raster layer")

    @staticmethod
    def get_safe_pk_where(
        info: LayerInfo,
        pk_values: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """
        Build a safe WHERE clause for single or composite primary keys.
        """
        access = info.layer_accessibility
        pk_cols = access.pk_cols or []

        if not pk_cols:
            raise HTTPException(
                status_code=400,
                detail="Layer has no primary key columns configured",
            )

        if len(pk_cols) != len(pk_values):
            raise HTTPException(
                status_code=400,
                detail="PK column count and value count do not match",
            )

        clauses: list[str] = []
        params: dict[str, Any] = {}

        for i, col in enumerate(pk_cols):
            pname = f"pk_{i}"
            clauses.append(f"{DBQuery.safe_ident(col)} = :{pname}")
            params[pname] = pk_values[i]

        return " AND ".join(clauses), params

    @staticmethod
    def validate_requested_columns(
        info: LayerInfo,
        cols: list[str] | None,
    ) -> list[str]:
        """
        Validate requested columns against configured allowed columns.
        """
        access = info.layer_accessibility
        allowed = set(access.cols_name or [])
        pk_cols = access.pk_cols or []
        geom_col = access.geometry_col

        allowed_all = set(allowed)
        allowed_all.update(pk_cols)

        if geom_col:
            allowed_all.add(geom_col)

        if not cols:
            return list(allowed)

        bad = [c for c in cols if c not in allowed_all]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid columns requested: {bad}",
            )

        return cols

    @staticmethod
    def df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
        """
        Convert DataFrame rows into JSON-safe records.
        """
        if df.empty:
            return []
        return df.where(df.notna(), None).to_dict(orient="records")

    @staticmethod
    def get_layer_fields(
        identifier: str,
        by: LayerLookupBy = "uuid",
    ) -> list[dict[str, Any]]:
        """
        Return configured field metadata from accessibility params.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        access = info.layer_accessibility

        if not access:
            return []

        return (access.params or {}).get("columns", [])

    @staticmethod
    def get_field_distinct_values(
            identifier: str,
            field_name: str,
            limit: int = 500,
            by: LayerLookupBy = "uuid",
            where_clause: str | None = None,
    ) -> list[Any]:
        """
        Return distinct values for a single allowed attribute column.
        Supports optional filtering via where_clause.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        LayerQueryService.ensure_vector_db_layer(info)

        access = info.layer_accessibility
        allowed_cols = set(access.cols_name or [])

        if field_name not in allowed_cols:
            raise HTTPException(status_code=400, detail="Invalid field name")

        df = info.to_layer_df(
            cols=[field_name],
            where_clause=where_clause,
            limit=-1,
            offset=0,
        )
        if df.empty or field_name not in df.columns:
            return []

        values = df[field_name].dropna().unique().tolist()
        return values[:limit]

    @staticmethod
    def get_layer_attributes(
            identifier: str,
            cols: list[str] | None = None,
            where_clause: str | None = None,
            limit: int = -1,
            offset: int = 0,
            by: LayerLookupBy = "uuid",
    ) -> dict[str, Any]:
        """
        Return attribute rows for a vector DB layer.
        Supports optional filtering through where_clause.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        LayerQueryService.ensure_vector_db_layer(info)

        safe_cols = LayerQueryService.validate_requested_columns(info, cols)

        total = info.count_layer_rows(where_clause=where_clause)

        df = info.to_layer_df(
            cols=safe_cols,
            where_clause=where_clause,
            limit=limit,
            offset=offset,
        )

        if df.empty:
            return {
                "columns": safe_cols,
                "rows": [],
                "total": total,
                "limit": limit,
                "offset": offset,
                "pkCols": info.layer_accessibility.pk_cols if info.layer_accessibility else [],
            }

        return {
            "columns": list(df.columns),
            "rows": LayerQueryService.df_to_records(df),
            "total": total,
            "limit": limit,
            "offset": offset,
            "pkCols": info.layer_accessibility.pk_cols if info.layer_accessibility else [],
        }

    @staticmethod
    def get_feature_detail(
        identifier: str,
        pk_values: list[str],
        by: LayerLookupBy = "uuid",
    ) -> dict[str, Any] | Response:
        """
        Return a single feature row by PK values.
        Geometry columns are removed from response payload.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        LayerQueryService.ensure_vector_db_layer(info)

        access = info.layer_accessibility
        where_sql, params = LayerQueryService.get_safe_pk_where(info, pk_values)

        engine = access.get_external_engine()
        if not engine:
            return Response(status_code=204)

        table = DBQuery.safe_ident(access.table_name)
        sql = text(f"SELECT * FROM {table} WHERE {where_sql} LIMIT 1")

        with Session(engine) as session:
            row = session.execute(sql, params).mappings().first()

        if not row:
            return Response(status_code=204)

        row_dict = dict(row)

        geom_col = access.geometry_col
        if geom_col:
            row_dict.pop(geom_col, None)

        row_dict.pop("geom", None)
        row_dict.pop("geometry", None)

        return row_dict

    @staticmethod
    def get_column_value(
        identifier: str,
        pk_values: list[str],
        col_name: str,
        by: LayerLookupBy = "uuid",
    ) -> Any:
        """
        Return a single column value from a single feature row.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        LayerQueryService.ensure_vector_db_layer(info)

        access = info.layer_accessibility
        allowed_cols = set(access.cols_name or [])

        if col_name not in allowed_cols:
            raise HTTPException(status_code=400, detail="Invalid column")

        where_sql, params = LayerQueryService.get_safe_pk_where(info, pk_values)

        engine = access.get_external_engine()
        if not engine:
            return ""

        table = DBQuery.safe_ident(access.table_name)
        target_col = DBQuery.safe_ident(col_name)

        sql = text(f"SELECT {target_col} FROM {table} WHERE {where_sql} LIMIT 1")

        with Session(engine) as session:
            val = session.execute(sql, params).scalar_one_or_none()

        return val if val is not None else ""

    @staticmethod
    def update_column_value(
        identifier: str,
        pk_values: list[str],
        col_name: str,
        value: Any,
        by: LayerLookupBy = "uuid",
    ) -> dict[str, Any]:
        """
        Update a single allowed column in a DB-backed vector layer row.
        """
        info = LayerQueryService.get_layer_info(identifier, by)
        LayerQueryService.ensure_vector_db_layer(info)

        access = info.layer_accessibility
        allowed_cols = set(access.cols_name or [])

        if col_name not in allowed_cols:
            raise HTTPException(status_code=400, detail="Invalid column")

        where_sql, params = LayerQueryService.get_safe_pk_where(info, pk_values)

        engine = access.get_external_engine()
        if not engine:
            raise HTTPException(
                status_code=400,
                detail="External engine not available",
            )

        table = DBQuery.safe_ident(access.table_name)
        target_col = DBQuery.safe_ident(col_name)

        params["new_value"] = value

        sql = text(
            f"UPDATE {table} "
            f"SET {target_col} = :new_value "
            f"WHERE {where_sql}"
        )

        with Session(engine) as session:
            result = session.execute(sql, params)
            session.commit()

        return {
            "updated": result.rowcount,
            "column": col_name,
            "value": value,
        }