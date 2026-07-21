import io
import json

from digitalarzengine.utils.singletons import da_logger
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from matplotlib import pyplot as plt, colorbar
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.dch.models import LayerInfo, MapInfo, DataModel, COGRaster, BandProcess
from app.damapql.dch.utils.dch_utils import DCHUtils
from app.damapql.dch.utils.mvt_utils import MVTCache

dch_styles_router = APIRouter(tags=["dch"])


def require_style_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(
        status_code=403,
        detail="Superuser or app_manager privileges required"
    )


@dch_styles_router.get("/get_style/{uuid}/{map_uuid}/")
async def get_style(uuid: str, map_uuid: str):
    """
    Retrieves style from map override or falls back to global layer style.
    Public endpoint.
    """
    style = None

    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            map_obj = session.execute(
                select(MapInfo).where(MapInfo.uuid == map_uuid)
            ).scalar_one_or_none()

            if map_obj and map_obj.layers:
                layer_entry = next(
                    (layer for layer in map_obj.layers if layer.get("uuid") == uuid),
                    None
                )
                if layer_entry:
                    style = layer_entry.get("style")

    if style is None or style == {}:
        with LayerInfo.managed_session() as session:
            layer_info = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if layer_info:
                style = layer_info.layer_styling

    return {"payload": style} if style else Response(status_code=204)


@dch_styles_router.post("/save_style/{uuid}/{map_uuid}/")
async def save_style(
        request: Request,
        uuid: str,
        map_uuid: str,
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Saves style either for a specific map-layer instance or globally for the layer.
    Protected endpoint.
    """
    require_style_manager(current_user)
    layer_styling = await request.json()

    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            obj = session.execute(
                select(MapInfo).where(MapInfo.uuid == map_uuid)
            ).scalar_one_or_none()

            if obj:
                new_layers = []
                for layer in (obj.layers or []):
                    if layer.get("uuid") == uuid:
                        layer["style"] = layer_styling
                    new_layers.append(layer)

                obj.layers = new_layers
                flag_modified(obj, "layers")
                obj.save(session=session)
                return {"payload": "Map Layer style saved successfully"}

    else:
        with LayerInfo.managed_session() as session:
            obj = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if obj:
                obj.layer_styling = layer_styling
                obj.save(session=session)

                # 👇 CLEAN CACHE: Force cache eviction so new styling takes immediate effect
                try:
                    cleared_tiles = MVTCache.clear_layer_cache(uuid)
                    da_logger.info(
                        f"[MVTCacheClear] Style updated globally. Cleared {cleared_tiles} tiles for layer {uuid}")
                except Exception as cache_err:
                    da_logger.error(f"[MVTCacheClearError] Failed to drop cache matrix: {cache_err}")

                return {"payload": "Global Layer style saved successfully"}

    return Response(status_code=204)


@dch_styles_router.post("/upload_sld_style/{uuid}/{map_uuid}/")
async def upload_sld_style(
    uuid: str,
    map_uuid: str,
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Uploads SLD or JSON styles.

    If map_uuid != "-1", saves style as map-specific layer style.
    If map_uuid == "-1", saves style as global LayerInfo.layer_styling.
    """
    require_style_manager(current_user)

    form = await request.form()
    file = form.get("file")

    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded")

    content = await file.read()

    filename = (getattr(file, "filename", "") or "").lower()
    content_type = (file.content_type or "").lower()

    is_json = (
        "json" in content_type
        or filename.endswith(".json")
        or content.lstrip().startswith((b"{", b"["))
    )

    # Parse uploaded style
    if is_json:
        try:
            parsed_style = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON style file")

    else:
        try:
            sld_str = content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid SLD style file")

        with LayerInfo.managed_session() as session:
            layer_obj = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if not layer_obj:
                raise HTTPException(status_code=404, detail="Layer not found")

            if layer_obj.data_model and layer_obj.data_model.code == DataModel.R.name:
                raster = layer_obj.to_raster()

                summary = BandProcess.get_summary_data(
                    raster.get_data_array(1),
                    nodata=raster.get_nodata_value(),
                )

                dtype = str(raster.get_profile().get("dtype", "")).lower()

                parsed_style = DCHUtils.raster_symbolizer_parser(
                    sld_str,
                    summary,
                    "float" in dtype,
                )
            else:
                parsed_style = {
                    "type": "sld",
                    "style": sld_str,
                }

    # Save map-specific style
    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            obj = session.execute(
                select(MapInfo).where(MapInfo.uuid == map_uuid)
            ).scalar_one_or_none()

            if not obj:
                raise HTTPException(status_code=404, detail="Map not found")

            found = False
            new_layers = []

            for layer in (obj.layers or []):
                layer_copy = dict(layer)

                if layer_copy.get("uuid") == uuid:
                    layer_copy["style"] = parsed_style
                    found = True

                new_layers.append(layer_copy)

            if not found:
                raise HTTPException(status_code=404, detail="Layer not found in map")

            obj.layers = new_layers
            flag_modified(obj, "layers")
            obj.save(session=session)

        return {"payload": "Map Layer style saved successfully"}

    # Save global/default layer style
    with LayerInfo.managed_session() as session:
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if not obj:
            raise HTTPException(status_code=404, detail="Layer not found")

        obj.layer_styling = parsed_style
        obj.save(session=session)

    # 👇 CLEAN CACHE: Clear stale generated imagery buffers for global assets updates
    try:
        cleared_tiles = MVTCache.clear_layer_cache(uuid)
        da_logger.info(f"[MVTCacheClear] SLD updated globally. Cleared {cleared_tiles} tiles for layer {uuid}")
    except Exception as cache_err:
        da_logger.error(f"[MVTCacheClearError] Failed to drop cache matrix: {cache_err}")

    return {"payload": "Global Layer style saved successfully"}


@dch_styles_router.get("/download_da_style/{uuid}/")
async def download_da_style(uuid: str):
    """
    Downloads the style for a layer as either JSON or SLD.
    Public endpoint.
    """
    with LayerInfo.managed_session() as session:
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if obj is not None and obj.layer_styling:
            layer_style = obj.layer_styling
            filename_base = obj.layer_name or f"style_{uuid}"

            if isinstance(layer_style, dict) and layer_style.get("type") == "sld":
                content = layer_style.get("style", "")
                media_type = "text/xml; charset=utf-8"
                extension = "sld"
            else:
                content = json.dumps(layer_style, indent=2)
                media_type = "application/json; charset=utf-8"
                extension = "json"

            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename_base}.{extension}"',
                    "Content-Length": str(len(content.encode("utf-8"))),
                },
            )

    return Response(status_code=204)


@dch_styles_router.post("/update_layer_visibility_zoom/{uuid}/")
async def update_layer_zoom(
        uuid: str,
        request: Request,
        current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Direct endpoint to save Layer visibility zoom constraints.
    Flushes tiles cache to update active client-side viewports cleanly.
    """
    # Enforce admin permission constraints
    require_style_manager(current_user)

    payload = await request.json()
    zoom_range = payload.get("zoom_range")

    # Validate that payload looks like a proper range array: [min, max]
    if not isinstance(zoom_range, list) or len(zoom_range) != 2:
        raise HTTPException(
            status_code=400,
            detail="Invalid payload. 'zoom_range' must be a list containing [min_zoom, max_zoom]"
        )

    try:
        with LayerInfo.managed_session() as session:
            obj = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if obj is None:
                raise HTTPException(status_code=404, detail="LayerInfo record not found")

            # Cast components safely to integers to prevent string evaluation anomalies
            obj.zoom_range = [int(zoom_range[0]), int(zoom_range[1])]

            # Explicitly flag the array change for SQLAlchemy ORM state tracking
            flag_modified(obj, "zoom_range")
            obj.save(session=session)

        # Clear active MVT map cache arrays so the tile delivery system respects the new scale boundaries immediately
        try:
            cleared_tiles = MVTCache.clear_layer_cache(uuid)
            da_logger.info(f"[MVTCacheClear] Zoom visibility updated. Evicted {cleared_tiles} tiles for {uuid}")
        except Exception as cache_err:
            da_logger.warning(
                f"[MVTCacheClearError] Failed to clear stale tile caches on zoom variance update: {cache_err}")

        return {"payload": {"status": "success", "zoom_range": obj.zoom_range}}

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to update visibility scale ranges on layer {uuid}: {e}")
        raise HTTPException(status_code=500, detail="Internal update processing error")

@dch_styles_router.get("/get_legend_graphic/{uuid}", response_class=Response)
@dch_styles_router.get("/get_legend_graphic/{uuid}/", response_class=Response)
async def get_legend_graphic(uuid: str):
    """
    Generates a PNG legend graphic from the saved layer style.
    Public endpoint.
    """
    try:
        with LayerInfo.managed_session() as session:
            info = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

        if not info or not info.layer_styling:
            raise HTTPException(status_code=404, detail="Layer style not found")

        style = info.layer_styling
        cp = COGRaster.create_color_map(style)

        if "labels" in style:
            labels = style["labels"]
        elif isinstance(cp, dict):
            labels = [str(k) for k in cp.keys()]
        elif isinstance(cp, list):
            labels = [str(round(v[0][0], 2)) for v in cp]
            if cp and cp[-1][0]:
                labels.append(str(round(cp[-1][0][-1], 2)))
        else:
            labels = []

        if not labels:
            raise HTTPException(status_code=400, detail="No labels available for legend")

        ticks = list(range(len(labels)))
        if len(ticks) != len(labels):
            raise HTTPException(status_code=400, detail="Mismatch between ticks and labels")

        is_horizontal = False
        fig: Figure = plt.figure(
            figsize=[500 / 96, 1] if is_horizontal else [500 / 96, 0.4 * len(labels)]
        )
        ax: Axes = fig.add_axes(
            [0.05, 0.8, 0.9, 0.8] if is_horizontal else [0.1, 0.15, 0.05, 0.8]
        )

        palette = style.get("palette")
        if not palette:
            raise HTTPException(status_code=400, detail="Palette is missing")

        if isinstance(palette, dict):
            palette = list(palette.values())

        if not isinstance(palette, list) or len(palette) < 2:
            raise HTTPException(status_code=400, detail="Invalid color palette")

        cmap = LinearSegmentedColormap.from_list("cdict", palette)

        cbar = colorbar.ColorbarBase(
            ax,
            orientation="horizontal" if is_horizontal else "vertical",
            cmap=cmap,
            norm=plt.Normalize(vmin=ticks[0], vmax=ticks[-1]),
        )

        if is_horizontal:
            cbar.ax.set_xticks(ticks)
            cbar.ax.set_xticklabels(labels)
        else:
            ax.tick_params(axis="y", which="major", labelsize=10)
            cbar.ax.set_yticks(ticks)
            cbar.ax.set_yticklabels(labels)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)

        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cross-Origin-Resource-Policy": "cross-origin"},
        )

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to generate legend graphic for uuid={uuid}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")