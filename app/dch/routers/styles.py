import io
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from matplotlib import pyplot as plt, colorbar
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from sqlalchemy import select

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
from app.dch.models import LayerInfo, MapInfo, DataModel, COGRaster, BandProcess
from utils.singletons import da_logger

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

    if style is None:
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
                obj.save(session=session, allow_update=True)
                return {"payload": "Map Layer style saved successfully"}

    else:
        with LayerInfo.managed_session() as session:
            obj = session.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()

            if obj:
                obj.layer_styling = layer_styling
                obj.save(session=session, allow_update=True)
                return {"payload": "Global Layer style saved successfully"}

    return Response(status_code=204)


@dch_styles_router.post("/upload_sld_style/{uuid}/")
async def upload_sld_style(
    uuid: str,
    request: Request,
    current_user: UserInfo = Depends(AuthUtils.get_current_user),
):
    """
    Uploads SLD or JSON styles and stores them in LayerInfo.layer_styling.
    Protected endpoint.
    """
    require_style_manager(current_user)

    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded")

    content = await file.read()

    with LayerInfo.managed_session() as session:
        obj = session.execute(
            select(LayerInfo).where(LayerInfo.uuid == uuid)
        ).scalar_one_or_none()

        if not obj:
            raise HTTPException(status_code=404, detail="Layer not found")

        if "json" in (file.content_type or "").lower():
            try:
                obj.layer_styling = json.loads(content)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON style file")
        else:
            sld_str = content.decode("utf-8")

            if obj.data_model and obj.data_model.code == DataModel.R.name:
                raster = obj.to_raster()
                summary = BandProcess.get_summary_data(
                    raster.get_data_array(1),
                    nodata=raster.get_nodata_value()
                )
                obj.layer_styling = DCHUtils.raster_symbolizer_parser(
                    sld_str,
                    summary,
                    "float" in raster.get_profile()["dtype"]
                )
            else:
                obj.layer_styling = {"type": "sld", "style": sld_str}

        obj.save(session=session, allow_update=True)

    return {"payload": "Style saved successfully"}


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