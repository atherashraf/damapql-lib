# app/damapql/config_registry/boundaries/service.py
from __future__ import annotations
from typing import Any, Tuple, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.damapql.dch.models import DBQuery
from app.damapql.utils.adapters.db import DBBase
from app.damapql.config_registry.boundaries.base import BaseBoundaryHierarchy
from app.damapql.config_registry.boundaries.schemas import BoundaryLevelConfig, BoundaryModeConfig


class BoundaryHierarchyService:
    @staticmethod
    def get_modes() -> list[BoundaryModeConfig]:
        impl = BaseBoundaryHierarchy.get_implementation()
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

        # Resolve correct DB connection (level -> mode -> fallback to default session)
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

        # Execute query on the resolved database engine
        if target_db_key:
            engine = DBBase.get_engine(db_key=target_db_key)
            with Session(engine) as session:
                rows = session.execute(sql, params).mappings().all()
                return [dict(r) for r in rows]
        else:
            rows = default_session.execute(sql, params).mappings().all()
            return [dict(r) for r in rows]