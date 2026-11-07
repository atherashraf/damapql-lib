# app/damapql/config_registry/boundaries/service.py
from __future__ import annotations
from typing import Any, Tuple, Optional

from digitalarzengine.utils.singletons import da_logger
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.damapql.dch.models import DBQuery
from app.damapql.utils.adapters.db import DBBase
from app.damapql.config_registry.boundaries.base import BaseBoundaryHierarchy
from app.damapql.config_registry.boundaries.schemas import BoundaryLevelConfig, BoundaryModeConfig


class BoundaryHierarchyService:
    @staticmethod
    def get_model() -> type[BaseBoundaryHierarchy]:
        return BaseBoundaryHierarchy.get_implementation()

    @staticmethod
    def get_modes() -> list[BoundaryModeConfig]:
        impl = BoundaryHierarchyService.get_model()
        return impl.get_modes()

    @staticmethod
    def get_mode_and_level_config(mode_key: str, level: int) -> tuple[BoundaryModeConfig, BoundaryLevelConfig]:
        modes = {m.mode_key: m for m in BoundaryHierarchyService.get_modes()}
        if mode_key not in modes:
            raise HTTPException(status_code=400, detail=f"Mode '{mode_key}' is not registered")

        mode_cfg = modes[mode_key]
        levels = {lvl.level: lvl for lvl in mode_cfg.levels}
        if level not in levels:
            raise HTTPException(status_code=400, detail=f"Level {level} not defined in mode '{mode_key}'")

        return mode_cfg, levels[level]

    @staticmethod
    def get_options(
        default_session: Session,
        mode_key: str,
        level: int,
        parent_id: Optional[str | int] = None,
    ) -> list[dict[str, Any]]:
        mode_cfg, lvl_cfg = BoundaryHierarchyService.get_mode_and_level_config(mode_key, level)

        target_db_key = lvl_cfg.db_key or mode_cfg.db_key

        schema = DBQuery.safe_ident(lvl_cfg.schema_name or "public")
        table = DBQuery.safe_ident(lvl_cfg.table_name)
        full_table = f"{schema}.{table}"

        pk_col = DBQuery.safe_ident(lvl_cfg.pk_col)
        label_col = DBQuery.safe_ident(lvl_cfg.label_col)

        where_clauses: list[str] = []
        params: dict[str, Any] = {}

        if lvl_cfg.where_clause:
            where_clauses.append(lvl_cfg.where_clause)

        if lvl_cfg.parent_fk_col:
            if parent_id is None:
                return []
            parent_col = DBQuery.safe_ident(lvl_cfg.parent_fk_col)
            where_clauses.append(f"{parent_col} = :parent_id")
            params["parent_id"] = parent_id

        where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = text(f"SELECT {pk_col} AS key, {label_col} AS label FROM {full_table} {where_str} ORDER BY {label_col}")

        if target_db_key:
            engine = DBBase.get_engine(db_key=target_db_key)
            with Session(engine) as session:
                rows = session.execute(sql, params).mappings().all()
                return [dict(r) for r in rows]
        else:
            rows = default_session.execute(sql, params).mappings().all()
            return [dict(r) for r in rows]

    @classmethod
    def get_boundary_wkt(cls, layer_name: str, spatial_id: str | int) -> Optional[str]:
        """
        Fetch boundary geometry WKT in EPSG:3857 for any registered boundary level
        (e.g., table_name='district', spatial_id=18).
        """
        impl = cls.get_model()

        # 1. Delegate to concrete implementation if it defines custom logic
        if hasattr(impl, "get_boundary_wkt"):
            try:
                res = impl.get_boundary_wkt(layer_name, spatial_id)
                if res:
                    return str(res)
            except NotImplementedError:
                pass

        # 2. Dynamic schema-driven fallback from registered modes
        modes = cls.get_modes()
        target_mode = None
        target_lvl = None

        clean_target = layer_name.lower().strip()
        for m in modes:
            for lvl in m.levels:
                if (
                    lvl.table_name.lower() == clean_target
                    or lvl.name.lower() == clean_target
                ):
                    target_mode = m
                    target_lvl = lvl
                    break
            if target_lvl:
                break

        if not target_lvl or not target_mode:
            return None

        target_db_key = target_lvl.db_key or target_mode.db_key
        schema = DBQuery.safe_ident(target_lvl.schema_name or "public")
        table = DBQuery.safe_ident(target_lvl.table_name)
        full_table = f"{schema}.{table}"
        pk_col = DBQuery.safe_ident(target_lvl.pk_col or "id")
        geom_col = DBQuery.safe_ident(getattr(target_lvl, "geom_col", None) or "geom")

        # Guarantee EPSG:3857 WKT output
        sql = text(
            f"SELECT ST_AsText(ST_Transform({geom_col}, 3857)) "
            f"FROM {full_table} WHERE {pk_col} = :spatial_id"
        )

        try:
            engine = DBBase.get_engine(db_key=target_db_key) if target_db_key else DBBase.get_engine()
            # Convert spatial_id to int if numeric
            param_id = int(spatial_id) if str(spatial_id).isdigit() else spatial_id
            with Session(engine) as session:
                return session.execute(sql, {"spatial_id": param_id}).scalar_one_or_none()
        except Exception as e:
            da_logger.exception(f"Failed to fetch boundary WKT for {layer_name} (ID: {spatial_id}): {e}")
            return None