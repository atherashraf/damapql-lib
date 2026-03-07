import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
from app.dch.models import LayerInfo, DataModel, COGRaster, DBQuery
from settings import MEDIA_DIR
from utils.adapters.db import DBBase
from utils.singletons import da_logger

dch_tiles_router = APIRouter(prefix="/dch", tags=["dch"])


_UUID_TOKEN_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def require_tile_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


def cache_table_name(uuid: str) -> str:
    """
    Returns a safe schema-qualified cache table name.
    Example: main."6f4b..._xyz"
    """
    if not _UUID_TOKEN_RE.fullmatch(uuid):
        raise HTTPException(status_code=400, detail="Invalid layer uuid")

    token = uuid.replace("-", "")
    return f'main."{token}_xyz"'


def ensure_cache_table(session: Session, uuid: str) -> str:
    """
    Ensures the MVT cache table exists for the given layer UUID.
    Returns the safe table name.
    """
    xyz_table = cache_table_name(uuid)
    token = uuid.replace("-", "")
    index_name = f'{token}_xyz_idx'

    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {xyz_table} (
            x INTEGER NOT NULL,
            y INTEGER NOT NULL,
            z INTEGER NOT NULL,
            mvt BYTEA NOT NULL,
            PRIMARY KEY (x, y, z)
        )
    """))

    session.execute(text(
        f'CREATE INDEX IF NOT EXISTS "{index_name}" ON {xyz_table} (z, x, y)'
    ))

    session.commit()
    return xyz_table


@dch_tiles_router.get(
    "/raster_tile/{uuid}/{z}/{x}/{y}/",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
@dch_tiles_router.get(
    "/raster_tile/{uuid}/{z}/{x}/{y}",
    responses={200: {"content": {"image/png": {}}}},
    response_class=Response,
)
async def get_raster_tile(uuid: str, z: int, x: int, y: int):
    """
    Public endpoint to serve raster XYZ/TMS tiles as PNG.
    """
    tile_size = 256

    with LayerInfo.managed_session() as session:
        try:
            info = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if info is None or not info.data_model or info.data_model.code != DataModel.R.name:
                raise HTTPException(status_code=404, detail="Layer not found or not raster")

            params = info.layer_accessibility.params or {}
            file_path = params.get("file_path")
            if not file_path:
                raise HTTPException(status_code=404, detail="Raster path missing in params")

            is_media_rel = str(params.get("is_path_media_rel", "")).lower() in ("true", "1", "yes")

            if file_path.startswith("s3://"):
                file_path = file_path[len("s3://"):]
                is_media_rel = True

            file_path = os.path.join(MEDIA_DIR, file_path) if is_media_rel else file_path

            cog_raster = COGRaster.open_from_local(file_path)
            color_map = COGRaster.create_color_map(info.layer_styling) if info.layer_styling else None

            data = cog_raster.read_tile_as_png(x, y, z, color_map=color_map, tile_size=tile_size)
            return Response(content=data.getvalue(), media_type="image/png")

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(f"[RasterTileError] uuid={uuid}, z={z}, x={x}, y={y} - {e}")
            empty_tile = COGRaster.create_empty_image(tile_size, tile_size)
            return Response(content=empty_tile.getvalue(), media_type="image/png")


@dch_tiles_router.get("/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}")
async def get_layer_mvt(
    uuid: str,
    tile_size: int,
    z: int,
    x: int,
    y: int,
    resolution: float = 0,
    cols: list[str] | None = Query(None),
):
    """
    Public endpoint to serve Mapbox Vector Tiles (MVT) with per-layer DB cache.
    """
    info = LayerInfo.get_layer_info_by_uuid(uuid)
    if not info:
        raise HTTPException(status_code=404, detail="Layer not found")

    access = info.layer_accessibility
    if not access:
        return Response(status_code=204)

    cache_engine = DBBase.get_engine(app_label="cache_db")

    # 1) cache read
    with Session(cache_engine) as cache_session:
        xyz_table = ensure_cache_table(cache_session, uuid)

        cached = cache_session.execute(
            text(f"SELECT mvt FROM {xyz_table} WHERE x=:x AND y=:y AND z=:z"),
            {"x": x, "y": y, "z": z},
        ).scalar_one_or_none()

        if cached:
            return Response(
                content=bytes(cached),
                media_type="application/vnd.mapbox-vector-tile"
            )

    # 2) generate on cache miss
    try:
        external_engine = access.get_external_engine()
        if not external_engine:
            return Response(status_code=204)

        allowed_cols = set(access.cols_name or [])
        requested_cols = cols or list(allowed_cols)

        bad = [c for c in requested_cols if c not in allowed_cols]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid columns requested: {bad}")

        select_cols = ", ".join(DBQuery.safe_ident(c) for c in requested_cols)
        geom_col = DBQuery.safe_ident(access.geometry_col)
        table = DBQuery.safe_ident(access.table_name)

        stmt = (
            f"SELECT {select_cols}, {geom_col} FROM {table}"
            if select_cols else
            f"SELECT {geom_col} FROM {table}"
        )

        with Session(external_engine) as ext_session:
            mvt_bytes = DBQuery.execute_query_as_mvt(
                session=ext_session,
                stmt=stmt,
                z=z,
                x=x,
                y=y,
                geom_col=access.geometry_col,
                layer_name=info.layer_name or "layer",
                extent=tile_size,
                resolution=resolution,
            )

        # 3) cache write
        if mvt_bytes:
            with Session(cache_engine) as cache_session:
                xyz_table = ensure_cache_table(cache_session, uuid)

                cache_session.execute(
                    text(
                        f"INSERT INTO {xyz_table} (x, y, z, mvt) "
                        f"VALUES (:x, :y, :z, :mvt) "
                        f"ON CONFLICT (x, y, z) DO UPDATE SET mvt = EXCLUDED.mvt"
                    ),
                    {"x": x, "y": y, "z": z, "mvt": mvt_bytes},
                )
                cache_session.commit()

        return Response(
            content=mvt_bytes or b"",
            media_type="application/vnd.mapbox-vector-tile"
        )

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"[MVTError] uuid={uuid}, z={z}, x={x}, y={y} - {e}")
        return Response(status_code=500)


@dch_tiles_router.delete("/clear_cache/{uuid}/")
async def clear_cache(
    uuid: str,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Protected endpoint to clear cached vector tiles for a layer.
    """
    require_tile_manager(current_user)

    try:
        cache_engine = DBBase.get_engine(app_label="cache_db")
    except KeyError:
        raise HTTPException(status_code=500, detail="Cache database configuration missing")

    xyz_table = cache_table_name(uuid)

    with Session(cache_engine) as session:
        try:
            session.execute(text(f"DROP TABLE IF EXISTS {xyz_table}"))
            session.commit()
            return {"payload": f"Cache table {xyz_table} successfully cleared"}
        except Exception as e:
            session.rollback()
            da_logger.exception(f"Failed to clear cache for uuid={uuid}: {e}")
            return {"payload": "error clearing cache", "detail": str(e)}