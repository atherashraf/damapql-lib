import io
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from matplotlib import pyplot as plt, colorbar
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
from app.damapql.cache.mvt_cache import mvt_cache
from app.damapql.dch.models import LayerInfo, MapInfo, DataModel, COGRaster, BandProcess
from app.damapql.dch.utils.dch_utils import DCHUtils
from digitalarzengine.utils.singletons import da_logger

dch_styles_router = APIRouter(tags=["dch"])


def require_style_manager(current_user: UserInfo):
    if current_user.is_superuser:
        return

    if AuthUtils.has_role(current_user, "app_manager"):
        return

    raise HTTPException(status_code=403, detail="Superuser or app_manager privileges required")


@dch_styles_router.get("/get_style/{uuid}/{map_uuid}/")
async def get_style(uuid: str, map_uuid: str):
    """
    Retrieves style from map override or falls back to global layer style.
    Public endpoint.
    """
    style = None

    if map_uuid != "-1":
        with MapInfo.managed_session() as session:
            map_obj = session.execute(select(MapInfo).where(MapInfo.uuid == map_uuid)).scalar_one_or_none()

            if map_obj and map_obj.layers:
                layer_entry = next((layer for layer in map_obj.layers if layer.get("uuid") == uuid), None)
                if layer_entry:
                    style = layer_entry.get("style")

    if style is None or style == {}:
        with LayerInfo.managed_session() as session:
            layer_info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

            if layer_info:
                style = layer_info.layer_styling

    return {"payload": style} if style else Response(status_code=204)


@dch_styles_router.post("/save_style/{uuid}/{map_uuid}/")
async def save_style(request: Request, uuid: str, map_uuid: str,
                     current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Save styling for either:

    1. A specific map-layer instance, or
    2. The global LayerInfo definition.

    Global style changes invalidate the layer's MVT cache when
    cached MVT output depends on layer styling.
    """

    require_style_manager(current_user)

    layer_styling = await request.json()

    # -------------------------------------------------------------
    # MAP-SPECIFIC STYLE
    # -------------------------------------------------------------
    if map_uuid != "-1":

        with MapInfo.managed_session() as session:

            obj = session.execute(select(MapInfo).where(MapInfo.uuid == map_uuid)).scalar_one_or_none()

            if obj is None:
                return Response(status_code=204)

            new_layers = []

            for layer in obj.layers or []:

                if layer.get("uuid") == uuid:
                    layer["style"] = layer_styling

                new_layers.append(layer)

            obj.layers = new_layers

            flag_modified(obj, "layers", )

            obj.save(session=session, )

        return {"payload": "Map Layer style saved successfully"}

    # -------------------------------------------------------------
    # GLOBAL LAYER STYLE
    # -------------------------------------------------------------

    with LayerInfo.managed_session() as session:

        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

        if obj is None:
            return Response(status_code=204)

        obj.layer_styling = layer_styling

        obj.save(session=session, )

    # -------------------------------------------------------------
    # INVALIDATE MVT CACHE
    # -------------------------------------------------------------

    try:
        cleared_count = mvt_cache.clear_cache(layer_uuid=uuid, )

        da_logger.info(f"[MVTCacheClear] "
                       f"Global style updated | "
                       f"layer={uuid} | "
                       f"cleared={cleared_count}")

    except Exception as cache_err:
        # Cache failure must not make style saving fail.
        da_logger.exception(f"[MVTCacheClearError] "
                            f"Style saved but cache invalidation failed | "
                            f"layer={uuid}: {cache_err}")

    return {"payload": "Global Layer style saved successfully"}


@dch_styles_router.post("/upload_sld_style/{uuid}/{map_uuid}/")
async def upload_sld_style(uuid: str, map_uuid: str, request: Request,
                           current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Upload SLD or JSON style.

    Behavior:
    - map_uuid != "-1":
        Save style for the layer instance inside the map.

    - map_uuid == "-1":
        Save style as the global/default LayerInfo style.

    Cache note:
    - Vector MVT cache is NOT invalidated because MVT contains
      geometry/attributes rather than rendered styling.
    - Raster/TMS/WMS cache invalidation should be handled by the
      corresponding cache implementation when those caches are used.
    """

    require_style_manager(current_user)

    form = await request.form()
    file = form.get("file")

    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded", )

    content = await file.read()

    filename = (getattr(file, "filename", "") or "").lower()

    content_type = (file.content_type or "").lower()

    is_json = ("json" in content_type or filename.endswith(".json") or content.lstrip().startswith((b"{", b"[")))

    # -------------------------------------------------------------
    # PARSE JSON STYLE
    # -------------------------------------------------------------

    if is_json:
        try:
            parsed_style = json.loads(content.decode("utf-8"))

        except (UnicodeDecodeError, json.JSONDecodeError,):
            da_logger.exception(f"Failed to parse JSON style file: "
                                f"{filename}")

            raise HTTPException(status_code=400, detail="Invalid JSON style file", )

    # -------------------------------------------------------------
    # PARSE SLD STYLE
    # -------------------------------------------------------------

    else:
        try:
            sld_str = content.decode("utf-8")

        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid SLD style file", )

        with LayerInfo.managed_session() as session:

            layer_obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

            if layer_obj is None:
                raise HTTPException(status_code=404, detail="Layer not found", )

            # -----------------------------------------------------
            # RASTER STYLE
            # -----------------------------------------------------

            if (layer_obj.data_model and layer_obj.data_model.code == DataModel.R.name):
                raster = layer_obj.to_raster()

                summary = BandProcess.get_summary_data(raster.get_data_array(1), nodata=raster.get_nodata_value(), )

                dtype = str(raster.get_profile().get("dtype", "", )).lower()

                parsed_style = (DCHUtils.raster_symbolizer_parser(sld_str, summary, "float" in dtype, ))

            # -----------------------------------------------------
            # VECTOR SLD STYLE
            # -----------------------------------------------------

            else:
                parsed_style = {"type": "sld", "style": sld_str, }

    # =============================================================
    # MAP-SPECIFIC STYLE
    # =============================================================

    if map_uuid != "-1":

        with MapInfo.managed_session() as session:

            obj = session.execute(select(MapInfo).where(MapInfo.uuid == map_uuid)).scalar_one_or_none()

            if obj is None:
                raise HTTPException(status_code=404, detail="Map not found", )

            found = False
            new_layers = []

            for layer in obj.layers or []:
                layer_copy = dict(layer)

                if layer_copy.get("uuid") == uuid:
                    layer_copy["style"] = parsed_style
                    found = True

                new_layers.append(layer_copy)

            if not found:
                raise HTTPException(status_code=404, detail="Layer not found in map", )

            obj.layers = new_layers

            flag_modified(obj, "layers", )

            obj.save(session=session, )

        return {"payload": "Map Layer style saved successfully"}

    # =============================================================
    # GLOBAL / DEFAULT STYLE
    # =============================================================

    with LayerInfo.managed_session() as session:

        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

        if obj is None:
            raise HTTPException(status_code=404, detail="Layer not found", )

        obj.layer_styling = parsed_style

        obj.save(session=session, )

    da_logger.info(f"[LayerStyleUpdated] "
                   f"Global style updated | "
                   f"layer={uuid} | "
                   f"file={filename or 'unknown'}")

    return {"payload": "Global Layer style saved successfully"}


@dch_styles_router.get("/download_da_style/{uuid}/")
async def download_da_style(uuid: str):
    """
    Downloads the style for a layer as either JSON or SLD.
    Public endpoint.
    """
    with LayerInfo.managed_session() as session:
        obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

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

            return Response(content=content, media_type=media_type,
                            headers={"Content-Disposition": f'attachment; filename="{filename_base}.{extension}"',
                                     "Content-Length": str(len(content.encode("utf-8"))), }, )

    return Response(status_code=204)


@dch_styles_router.post("/update_layer_visibility_zoom/{uuid}/")
async def update_layer_zoom(uuid: str, request: Request,
        current_user: UserInfo = Depends(AuthUtils.get_current_user), ):
    """
    Update layer visibility zoom constraints.

    MVT cache is not invalidated because zoom_range controls
    tile availability, not tile content.

    The MVT endpoint must enforce zoom_range before performing
    the Redis cache lookup.
    """

    require_style_manager(current_user)

    payload = await request.json()
    zoom_range = payload.get("zoom_range")

    if (not isinstance(zoom_range, list) or len(zoom_range) != 2):
        raise HTTPException(status_code=400, detail=("Invalid payload. 'zoom_range' must be a list "
                                                     "containing [min_zoom, max_zoom]"), )

    try:
        min_zoom = int(zoom_range[0])
        max_zoom = int(zoom_range[1])

        if min_zoom < 0 or max_zoom < 0:
            raise HTTPException(status_code=400, detail="Zoom values cannot be negative", )

        if min_zoom > max_zoom:
            raise HTTPException(status_code=400, detail="Minimum zoom cannot be greater than maximum zoom", )

        new_zoom_range = [min_zoom, max_zoom, ]

        with LayerInfo.managed_session() as session:
            obj = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

            if obj is None:
                raise HTTPException(status_code=404, detail="LayerInfo record not found", )

            obj.zoom_range = new_zoom_range

            flag_modified(obj, "zoom_range", )

            obj.save(session=session, )

        da_logger.info(f"[LayerZoomUpdated] "
                       f"layer={uuid} | "
                       f"zoom_range={new_zoom_range}")

        return {"payload": {"status": "success", "zoom_range": new_zoom_range, }}

    except HTTPException:
        raise

    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Zoom range values must be valid integers", )

    except Exception as err:
        da_logger.exception(f"Failed to update visibility zoom range "
                            f"for layer {uuid}: {err}")

        raise HTTPException(status_code=500, detail="Internal update processing error", )


@dch_styles_router.get("/get_legend_graphic/{uuid}", response_class=Response)
@dch_styles_router.get("/get_legend_graphic/{uuid}/", response_class=Response)
async def get_legend_graphic(uuid: str):
    """
    Generates a PNG legend graphic from the saved layer style.
    Falls back to a dynamic Black & White min/max legend if no custom style is defined.
    Public endpoint.
    """
    try:
        with LayerInfo.managed_session() as session:
            info = session.execute(select(LayerInfo).where(LayerInfo.uuid == uuid)).scalar_one_or_none()

            if not info:
                raise HTTPException(status_code=404, detail="Layer not found")

            style = info.layer_styling or {}
            cp = COGRaster.create_color_map(style) if style else None

            labels = []
            raw_palette = []

            # --- PATH A: Custom Styling Exists ---
            if cp or style:
                if "labels" in style and isinstance(style["labels"], list):
                    labels = [str(lbl) for lbl in style["labels"]]
                elif isinstance(cp, list) and len(cp) > 0:
                    labels = [str(round(v[0][0], 2)) for v in cp if isinstance(v[0], (tuple, list))]
                    if isinstance(cp[-1][0], (tuple, list)) and cp[-1][0][1] != float("inf"):
                        labels.append(str(round(cp[-1][0][1], 2)))
                elif isinstance(cp, dict) and len(cp) > 0:
                    labels = [str(k) for k in cp.keys()]

                raw_palette = (style.get("palette") or style.get("colors") or style.get("color_ramp"))

                if not raw_palette and isinstance(cp, list):
                    raw_palette = [f"#{r:02x}{g:02x}{b:02x}" for _, (r, g, b, *_) in cp]

            # Convert raw_palette into a clean list of colors regardless of input structure
            palette = []
            if isinstance(raw_palette, dict):
                # If dictionary keys are numeric/sortable, preserve sorted order
                try:
                    sorted_keys = sorted(raw_palette.keys(), key=lambda k: float(k))
                    palette = [raw_palette[k] for k in sorted_keys]
                except (ValueError, TypeError):
                    palette = list(raw_palette.values())
            elif isinstance(raw_palette, (list, tuple)):
                palette = list(raw_palette)

            # --- PATH B: Fallback Grayscale Mode (Matches Tile Renderer Min/Max) ---
            if not labels or not palette:
                min_v, max_v = 0.0, 100.0
                try:
                    # raster_path = getattr(info, "file_path", None) or getattr(info, "s3_path", None)

                    # if info.layer_accessibility.is_file:
                    #     raster_path = getattr(info.layer_accessibility, "file_path", None)
                    #     cog = COGRaster.open_cog(raster_path)
                    #     min_v, max_v = cog.global_minmax
                    # elif hasattr(info, "to_raster"):
                    #     cog = info.to_raster()
                    #     min_v, max_v = cog.global_minmax
                    cog_raster = info.to_cog()
                    min_v, max_v = cog_raster.global_minmax
                except Exception as err:
                    da_logger.exception(f"Failed to fetch min/max from raster for legend: {err}")

                labels = [str(round(min_v, 2)), str(round(max_v, 2))]
                palette = ["#000000", "#FFFFFF"]  # Black to White gradient

            ticks = list(range(len(labels)))
            is_horizontal = False

            fig: Figure = plt.figure(
                figsize=[500 / 96, 1] if is_horizontal else [500 / 96, max(0.4 * len(labels), 1.2)], facecolor="white"
                # Ensure figure backdrop is white
            )
            ax: Axes = fig.add_axes([0.05, 0.8, 0.9, 0.8] if is_horizontal else [0.1, 0.15, 0.05, 0.8])
            ax.set_facecolor("white")  # Ensure axes backdrop is white

            cmap = LinearSegmentedColormap.from_list("cdict", palette)

            cbar = colorbar.ColorbarBase(ax, orientation="horizontal" if is_horizontal else "vertical", cmap=cmap,
                                         norm=plt.Normalize(vmin=ticks[0], vmax=ticks[-1]), )

            if is_horizontal:
                cbar.ax.set_xticks(ticks)
                cbar.ax.set_xticklabels(labels)
            else:
                ax.tick_params(axis="y", which="major", labelsize=10)
                cbar.ax.set_yticks(ticks)
                cbar.ax.set_yticklabels(labels)

            buf = io.BytesIO()
            # Changed transparent=True to facecolor="white"
            fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
            plt.close(fig)

            return Response(content=buf.getvalue(), media_type="image/png",
                            headers={"Cross-Origin-Resource-Policy": "cross-origin"}, )

    except HTTPException:
        raise
    except Exception as e:
        da_logger.exception(f"Failed to generate legend graphic for uuid={uuid}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
