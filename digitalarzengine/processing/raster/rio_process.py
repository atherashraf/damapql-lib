from __future__ import annotations


import os
import traceback
from pathlib import Path
import folium
import matplotlib.cm as cm
import matplotlib.colors as colors
import base64
import io
import webbrowser
import tempfile

import rasterio
from rasterio.io import MemoryFile
from affine import Affine

from PIL import Image
from geopandas import GeoDataFrame
from matplotlib import pyplot as plt, gridspec
from mpl_toolkits.axes_grid1 import make_axes_locatable
from rasterio import DatasetReader, Env
from rasterio.merge import merge
from scipy.stats import genextreme
from typing import List, Optional, Sequence, Union, Dict, Any, Literal
import numpy as np
import pandas as pd
import geopandas as gpd
from rasterio.mask import mask
from matplotlib.colors import ListedColormap, BoundaryNorm
import matplotlib.patches as mpatches


from digitalarzengine.io.file_io import FileIO
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.utils.singletons import da_logger


class RioProcess:

    @staticmethod
    def read_raster_ds(img_folder: str):
        ds_files: [DatasetReader] = []
        path = Path(img_folder)
        issues_folder = os.path.join(img_folder, "issue_in_files")
        os.makedirs(issues_folder,exist_ok=True)
        # count = FileIO.get_file_count(img_folder)
        # test = [str(p) for p in path.iterdir() if p.suffix == ".tif"]
        # ds_files = []
        for p in path.iterdir():
            if p.suffix == ".tif":
                try:
                    ds_files.append(RioRaster(str(p)).get_dataset())
                except Exception as e:
                    traceback.print_exc()
                    print(str(e))
                    FileIO.mvFile(str(p), issues_folder)
        return ds_files

    # @classmethod
    # def mosaic_images(cls, img_folder: str = None, ds_files: [DatasetReader] = (),  ext="tif") -> RioRaster:
    #     is_limit_changed = False
    #     if img_folder is not None:
    #         # count = FileIO.get_file_count(img_folder)
    #         count = FileIO.get_file_count(img_folder)
    #         # get file reading limits
    #         soft, hard = FileIO.get_file_reading_limit()
    #         # print("soft", soft, "hard", hard)
    #         if count > soft:
    #             if count * 2 < hard:
    #                 """
    #                 default limit is  soft: 12544 hard:9223372036854775807
    #                 """
    #                 FileIO.set_file_reading_limit(count * 2)
    #
    #                 is_limit_changed = True
    #             else:
    #                 raise IOError(f"you are trying to read {count} files. Cannot read more than {hard} files.")
    #         ds_files = cls.read_raster_ds(img_folder)
    #         # problem_files.append(str(p))
    #     if len(ds_files) > 0:
    #         with Env(CHECK_DISK_FREE_SPACE=False):
    #             mosaic, out_trans = merge(ds_files)
    #             crs = ds_files[0].crs
    #             raster = RioRaster.raster_from_array(mosaic, crs=crs, g_transform=out_trans)
    #         if is_limit_changed:
    #             FileIO.set_file_reading_limit(soft)
    #         return raster

    @classmethod
    def mosaic_images(
            cls,
            img_folder: str = None,
            ds_files: list = (),
            ext: str = "tif",
            nodata_value=None,
    ) -> RioRaster:
        """
        Create a mosaic from multiple rasters.

        Features:
        - Reads rasters from a folder or an existing list of datasets.
        - Temporarily increases OS file descriptor limits when needed.
        - Fixes rasters with positive Y pixel size (upside-down rasters).
        - Preserves source raster dtype where possible.
        - Preserves source nodata values unless explicitly overridden.
        - Returns a RioRaster object.
        """

        # ------------------------------------------------------------------
        # Handle file descriptor limits when reading large raster collections
        # ------------------------------------------------------------------
        is_limit_changed = False
        soft = hard = None

        if img_folder is not None:
            count = FileIO.get_file_count(img_folder)

            # Current operating system limits for open files
            soft, hard = FileIO.get_file_reading_limit()

            # Increase soft limit if the number of rasters exceeds it
            if count > soft:
                if count * 2 < hard:
                    FileIO.set_file_reading_limit(count * 2)
                    is_limit_changed = True
                else:
                    raise IOError(
                        f"you are trying to read {count} files. "
                        f"Cannot read more than {hard} files."
                    )

            # Open all rasters in the folder
            ds_files = cls.read_raster_ds(img_folder)

        # ------------------------------------------------------------------
        # Nothing to mosaic
        # ------------------------------------------------------------------
        if not ds_files:
            return None

        corrected_ds_files = []
        mem_files = []

        try:

            # ------------------------------------------------------------------
            # Use first raster as reference for output datatype and nodata
            # ------------------------------------------------------------------
            out_dtype = ds_files[0].dtypes[0]
            src_nodata = ds_files[0].nodata

            # ------------------------------------------------------------------
            # Determine output nodata value
            #
            # Priority:
            # 1. User supplied nodata_value
            # 2. Source raster nodata
            # 3. Sensible default based on datatype
            # ------------------------------------------------------------------
            if nodata_value is None:
                if src_nodata is not None:
                    nodata_value = src_nodata
                else:
                    if np.issubdtype(np.dtype(out_dtype), np.floating):
                        nodata_value = np.nan
                    else:
                        nodata_value = 0

            # ------------------------------------------------------------------
            # Preprocess each raster before merge
            #
            # Tasks:
            # - Normalize nodata handling
            # - Fix upside-down rasters
            # - Create in-memory datasets for merge()
            # ------------------------------------------------------------------
            for ds in ds_files:

                t = ds.transform

                # Read all bands without changing datatype
                data = ds.read()

                current_dtype = data.dtype

                # --------------------------------------------------------------
                # Floating-point rasters
                #
                # Floating rasters may legitimately contain NaN values,
                # so we preserve and normalize them.
                # --------------------------------------------------------------
                if np.issubdtype(current_dtype, np.floating):

                    data = data.astype(current_dtype, copy=False)

                    # Convert source nodata pixels to output nodata
                    if ds.nodata is not None and not np.isnan(ds.nodata):
                        data[data == ds.nodata] = nodata_value

                    # Replace NaN values if output nodata is numeric
                    if not (
                            isinstance(nodata_value, float)
                            and np.isnan(nodata_value)
                    ):
                        data[np.isnan(data)] = nodata_value

                # --------------------------------------------------------------
                # Integer rasters
                #
                # Do NOT introduce NaN values because integer rasters
                # cannot represent NaN.
                # --------------------------------------------------------------
                else:
                    if ds.nodata is not None:
                        data[data == ds.nodata] = nodata_value

                # --------------------------------------------------------------
                # Fix upside-down rasters
                #
                # A positive transform.e indicates rows increase upward
                # instead of downward. Rasterio merge assumes a normal
                # north-up raster orientation.
                # --------------------------------------------------------------
                if t.e > 0:

                    print(f"Fixing upside-down raster: {ds.name}")

                    # Flip rows
                    data = data[:, ::-1, :]

                    # Rebuild transform after flipping
                    corrected_transform = Affine(
                        t.a, t.b, t.c,
                        t.d, -t.e, t.f + ds.height * t.e
                    )

                else:
                    corrected_transform = t

                # --------------------------------------------------------------
                # Create temporary in-memory raster
                #
                # merge() expects raster datasets, not raw arrays.
                # --------------------------------------------------------------
                profile = ds.profile.copy()

                profile.update(
                    dtype=str(data.dtype),
                    transform=corrected_transform,
                    nodata=nodata_value,
                    height=data.shape[1],
                    width=data.shape[2],
                )

                mem_file = MemoryFile()
                mem_ds = mem_file.open(**profile)
                mem_ds.write(data)

                corrected_ds_files.append(mem_ds)
                mem_files.append(mem_file)

            # ------------------------------------------------------------------
            # Merge all corrected rasters into a single mosaic
            # ------------------------------------------------------------------
            with Env(CHECK_DISK_FREE_SPACE=False):

                mosaic, out_trans = merge(
                    corrected_ds_files,
                    nodata=nodata_value,
                )

            # ------------------------------------------------------------------
            # Preserve original datatype where possible
            # ------------------------------------------------------------------
            mosaic = mosaic.astype(out_dtype, copy=False)

            # Reference CRS from first raster
            crs = corrected_ds_files[0].crs

            # ------------------------------------------------------------------
            # Convert merged array back into RioRaster
            # ------------------------------------------------------------------
            raster = RioRaster.raster_from_array(
                mosaic,
                crs=crs,
                g_transform=out_trans,
                nodata_value=nodata_value,
            )

            return raster

        finally:

            # ------------------------------------------------------------------
            # Cleanup temporary in-memory datasets
            # ------------------------------------------------------------------
            for ds in corrected_ds_files:
                try:
                    ds.close()
                except Exception:
                    pass

            # ------------------------------------------------------------------
            # Cleanup MemoryFile objects
            # ------------------------------------------------------------------
            for mem_file in mem_files:
                try:
                    mem_file.close()
                except Exception:
                    pass

            # ------------------------------------------------------------------
            # Restore original file descriptor limit if modified
            # ------------------------------------------------------------------
            if is_limit_changed:
                FileIO.set_file_reading_limit(soft)

    @staticmethod
    def get_return_period_surfaces(raster: RioRaster, output_path, return_periods=(2, 5, 10, 25, 50, 100)):

        data = raster.get_data_array()
        no_datavalue = raster.get_nodata_value()

        # Prepare output array (bands = number of return periods, height, width)
        return_level_raster = np.full((len(return_periods), data.shape[1], data.shape[2]), no_datavalue,
                                      dtype=np.float32)

        # GEV Fit for each pixel
        for i in range(data.shape[1]):  # Loop over rows
            for j in range(data.shape[2]):  # Loop over columns
                pixel_values = data[:, i, j]  # Extract yearly max precipitation for this pixel

                # Exclude nodata values
                valid_values = pixel_values[pixel_values != no_datavalue]

                if valid_values.size == 0 or np.all(np.isnan(valid_values)):  # Skip if no valid values
                    continue

                # Fit GEV distribution (shape, location, scale)
                shape, loc, scale = genextreme.fit(valid_values)

                # Compute return level for each return period
                for band_idx, rp in enumerate(return_periods):
                    return_level_raster[band_idx, i, j] = genextreme.ppf(1 - 1 / rp, shape, loc=loc, scale=scale)

        # Save the single raster with multiple bands

        profile = raster.get_profile().copy()
        profile.update(dtype=rasterio.float32, count=len(return_periods),
                       nodata=no_datavalue)  # Ensure nodata value is set

        with rasterio.open(output_path, "w", **profile) as dst:
            for band_idx, rp in enumerate(return_periods):
                dst.write(return_level_raster[band_idx], band_idx + 1)  # Write each return period to a band
                dst.set_band_description(band_idx + 1, f"Return Period {rp} years")  # Set band name

            dst.update_tags(return_periods=str(return_periods))  # Store metadata

        print(f"Return period Saved at : {output_path}")

    @staticmethod
    def _extent_from_transform(transform, width, height):
        xmin = transform.c
        ymax = transform.f
        xmax = xmin + transform.a * width
        ymin = ymax + transform.e * height
        # normalize so (xmin, xmax, ymin, ymax)
        return (min(xmin, xmax), max(xmin, xmax), min(ymin, ymax), max(ymin, ymax))

    @staticmethod
    def plot_categorical_raster(
            raster: RioRaster,
            title: str,
            output_fp: Union[str, Path],
            *,
            lookup_table: Union[pd.DataFrame, Dict[int, str]],
            value_col: str = "pixel_value",
            color_col: str = "color",
            label_col: str = "class_name",
            nodata_value: Optional[Union[int, float]] = None,
            gdf: Optional[GeoDataFrame] = None,
            edgecolor: str = "black",
            linewidth: float = 0.8,
            band_names: Optional[Sequence[Union[int, str]]] = None,
            band_titles: Optional[Sequence[str]] = None,

            legend: bool = True,
            legend_ncol: int = 3,
            legend_fontsize: int = 8,
            legend_only_present_classes: bool = True,

            label_field: Optional[str] = None,
            label_style: Optional[Dict[str, Any]] = None,
            label_min_dist_px: int = 20,
            label_max_count: Optional[int] = None,
            label_only_largest: bool = True
    ) -> Union[str, Path]:

        output_fp = Path(output_fp)
        output_fp.parent.mkdir(parents=True, exist_ok=True)

        # --------------------------------------------------
        # Normalize lookup table
        # --------------------------------------------------
        if isinstance(lookup_table, dict):
            values = sorted(list(lookup_table.keys()))
            labels = [lookup_table[v] for v in values]
            cmap_auto = plt.get_cmap("tab20", len(values))
            colors_ = [cmap_auto(i) for i in range(len(values))]
            lut = pd.DataFrame({
                value_col: values,
                color_col: colors_,
                label_col: labels
            })
        else:
            lut = lookup_table.copy()

        for col_name in (value_col, color_col, label_col):
            if col_name not in lut.columns:
                raise ValueError(f"lookup_table missing required column: {col_name!r}")

        lut[value_col] = lut[value_col].astype(int)
        lut = lut.sort_values(value_col)

        # --------------------------------------------------
        # Use only one band
        # --------------------------------------------------
        if band_names:
            b = band_names[0]
            if isinstance(b, int):
                band_idx = b
            elif isinstance(b, str):
                band_idx = None
                for i in range(1, raster.get_spectral_resolution() + 1):
                    if raster.get_band_name(i) == b:
                        band_idx = i
                        break
                if band_idx is None:
                    raise ValueError(f"Band name '{b}' not found.")
            else:
                raise TypeError("band_names must contain int or str.")
        else:
            band_idx = 1

        band = raster.get_data_array(band_idx)

        if nodata_value is None:
            nodata_value = raster.get_nodata_value()

        if nodata_value is not None:
            band_plot = np.ma.array(band, mask=(band == nodata_value))
        else:
            band_plot = band

        present_vals = set(np.unique(band).astype(int).tolist())

        if legend_only_present_classes:
            lut_plot = lut[lut[value_col].isin(present_vals)].copy()
        else:
            lut_plot = lut.copy()

        if lut_plot.empty:
            lut_plot = lut.copy()

        values = lut_plot[value_col].astype(int).to_list()
        colors_ = lut_plot[color_col].to_list()
        labels = lut_plot[label_col].astype(str).to_list()

        cmap = ListedColormap(colors_)
        boundaries = [v - 0.5 for v in values] + [values[-1] + 0.5]
        norm = BoundaryNorm(boundaries, cmap.N)

        # --------------------------------------------------
        # Plot one image + legend in same figure
        # --------------------------------------------------
        fig, ax = plt.subplots(figsize=(10, 10))

        transform = raster.get_geo_transform()
        h, w = band.shape
        extent = RioProcess._extent_from_transform(transform, w, h)

        ax.imshow(
            band_plot,
            cmap=cmap,
            norm=norm,
            interpolation="nearest",
            extent=extent,
            origin="upper",
            zorder=1,
        )

        raster_crs = raster.get_crs()

        if gdf is not None and not gdf.empty:
            if getattr(gdf, "crs", None) is not None and raster_crs is not None:
                if gdf.crs != raster_crs:
                    gdf = gdf.to_crs(raster_crs)

            gdf.boundary.plot(
                ax=ax,
                edgecolor=edgecolor,
                linewidth=linewidth,
                zorder=5,
            )

            if label_field and label_field in gdf.columns:
                style = {
                    "fontsize": 8,
                    "color": "black",
                    "ha": "center",
                    "va": "center",
                    "zorder": 10,
                    "bbox": dict(
                        boxstyle="round,pad=0.15",
                        fc="white",
                        ec="gray",
                        alpha=0.7,
                        linewidth=0.5,
                    ),
                }

                if label_style:
                    style.update(label_style)

                label_gdf = gdf[gdf[label_field].notna()].copy()

                if label_only_largest:
                    try:
                        label_gdf["_area_tmp"] = label_gdf.geometry.area
                        label_gdf = label_gdf.sort_values("_area_tmp", ascending=False)
                    except Exception:
                        pass

                if label_max_count is not None:
                    label_gdf = label_gdf.head(label_max_count)

                for _, row in label_gdf.iterrows():
                    if row.geometry is None or row.geometry.is_empty:
                        continue

                    p = row.geometry.representative_point()
                    ax.text(p.x, p.y, str(row[label_field]), **style)

        if title:
            ax.set_title(title, fontsize=12)

        ax.set_axis_off()

        if legend:
            handles = [
                mpatches.Patch(color=colors_[i], label=labels[i])
                for i in range(len(values))
            ]

            ax.legend(
                handles=handles,
                loc="lower center",
                bbox_to_anchor=(0.5, -0.12),
                fontsize=legend_fontsize,
                ncol=legend_ncol,
                frameon=False,
            )

        fig.savefig(
            output_fp,
            dpi=300,
            bbox_inches="tight",
            pad_inches=0.15,
        )

        plt.close(fig)

        return output_fp

    @staticmethod
    def create_plt_files_collage(plt_files: list[str], output_path: str, num_rows: int):
        # 1. Filter out only files that actually exist in your working directory
        valid_images = [img for img in plt_files if os.path.exists(img)]

        if not valid_images:
            raise FileNotFoundError("None of the specified images were found. Please check your file paths!")

        # Ensure num_rows is valid
        if num_rows <= 0:
            raise ValueError("Number of rows must be at least 1.")

        # Cap rows if you request more rows than there are actual images
        rows = min(num_rows, len(valid_images))

        # 2. Grid Layout configuration: Calculate columns based on the fixed rows
        # Using ceiling division to ensure all images fit
        cols = (len(valid_images) + rows - 1) // rows

        # 3. Open images and find uniform dimensions
        images = [Image.open(img) for img in valid_images]
        img_width, img_height = images[0].size

        # Resize them to match the first one to keep things clean
        resized_images = [img.resize((img_width, img_height)) for img in images]

        # 4. Create a blank canvas with a white background
        padding = 40
        top_margin = 100

        canvas_width = (img_width * cols) + (padding * (cols + 1))
        canvas_height = (img_height * rows) + (padding * (rows + 1)) + top_margin

        collage = Image.new("RGB", (canvas_width, canvas_height), color="white")

        # 5. Paste images onto the canvas
        for i, img in enumerate(resized_images):
            current_row = i // cols
            current_col = i % cols

            # Logic to center alignment on the final row if it is incomplete
            if current_row == rows - 1:
                # How many total images land on the very last row?
                images_on_last_row = len(resized_images) - (current_row * cols)

                # Find the starting x position to center the last row group
                row_width = (images_on_last_row * img_width) + ((images_on_last_row - 1) * padding)
                start_x = (canvas_width - row_width) // 2

                # Calculate x based on its relative position in that last row
                x = start_x + (current_col * (img_width + padding))
            else:
                x = padding + (current_col * (img_width + padding))

            y = top_margin + padding + (current_row * (img_height + padding))

            collage.paste(img, (x, y))

        # 6. Save the final masterpiece
        collage.save(output_path, "JPEG", quality=95)
        print(f"Success! Collage saved as '{output_path}' using {rows} row(s) and {cols} column(s).")


    @staticmethod
    def create_collage_images(
            raster: Union[RioRaster, str],
            title: str,
            output_fp: Union[str, Path],
            no_of_rows: int = 2,
            cmap: str = 'Blues',
            edgecolor: str = 'green',
            linewidth: float = 1.0,
            gdf: Optional[GeoDataFrame] = None,
            sub_titles: Optional[Sequence[str]] = None,
            vmin: Optional[Union[float, str]] = None,
            vmax: Optional[Union[float, str]] = None,

            band_names: Optional[Sequence[Union[int, str]]] = None,
            band_titles: Optional[Sequence[str]] = None,

            # ✅ NEW: labeling controls
            label_field: Optional[str] = None,  # e.g. "name" / "id"
            label_style: Optional[Dict[str, Any]] = None,  # matplotlib text kwargs
            label_min_dist_px: int = 18,  # declutter threshold (pixels)
            label_max_count: Optional[int] = None,  # cap labels per subplot
            label_only_largest: bool = False,  # label largest polys first
    ):
        """
        Create a multi-band image collage for a raster.

        This function visualizes each band of a raster in a grid layout. Bands are
        rendered using a shared colormap, optional vector boundaries, optional
        vector labels with automatic decluttering, and per-band color limits
        (absolute or percentile-based).

        Parameters
        ----------
        raster : RioRaster
            Raster object providing bands, metadata, transform, CRS, and nodata.
        title : str
            Main title prefix applied to each subplot (band name appended).
        output_fp : str
            Destination file path for the saved image (PNG, JPG, etc.).
        no_of_rows : int, default=2
            Number of rows in the figure grid. Columns are computed automatically.
        cmap : str, default='Blues'
            Matplotlib colormap name.
        edgecolor : str, default='green'
            Boundary color for overlaying vector geometries (if provided).
        linewidth : float, default=1.0
            Line thickness for vector boundaries.
        gdf : GeoDataFrame, optional
            Vector layer to plot on top of each band. Reprojected if CRS differs.
        sub_titles : Sequence[str], optional
            Custom subtitles for each band. Falls back to raster band names.
        vmin : float or str, optional
            Lower limit for color scaling. Can be:
                - a float (absolute value), e.g. 0
                - a percentile string, e.g. "5%" or "2.5%"
            Percentiles are computed **per band** ignoring nodata.
        vmax : float or str, optional
            Upper limit for color scaling. Same rules as `vmin`.

        band_names : sequence of int or str, optional
                Bands to include in the collage.
                - int values refer to 1-based band indices
                - str values refer to raster band names
                If None, all bands are plotted.

        band_titles : sequence of str, optional
                Custom titles for each plotted band (in plotting order).
                If provided, these override raster band names.
                If fewer titles than bands are given, remaining bands fall back
                to raster.get_band_name().


        Labeling Parameters
        -------------------
        label_field : str, optional
            Column name in `gdf` containing text labels to draw on the map.
            If None, no labels are rendered.
        label_style : dict, optional
            Matplotlib text style keyword arguments passed to `ax.text`.
            Defaults include:
                - fontsize=9
                - color="black"
                - centered alignment
                - semi-transparent white background box
        label_min_dist_px : int, default=18
            Minimum pixel distance between labels used for decluttering.
            Larger values result in fewer labels.
        label_max_count : int, optional
            Maximum number of labels drawn per subplot. Useful for very dense layers.
        label_only_largest : bool, default=False
            If True and geometries are polygons, labels are placed on the
            largest features first (by area).

        Notes
        -----
        - Each raster band is plotted in its own subplot.
        - Nodata values are converted to NaN and rendered using the colormap's
          "bad" color (white).
        - Percentile-based `vmin` / `vmax` values are computed independently
          for each band.
        - Vector labels are placed using representative points (polygons)
          or centroids (lines/points).
        - Label decluttering is performed in **pixel space**, ensuring consistent
          spacing regardless of map scale.
        - Subplots exceeding the number of raster bands are hidden automatically.

        Returns
        -------
        str
            The same `output_fp` provided, after saving the figure.

        Examples
        --------
        # Absolute color limits
        create_collage_images(
            raster,
            "My Raster",
            "out.png",
            vmin=0,
            vmax=500
        )

        # Percentile stretch (per band)
        create_collage_images(
            raster,
            "My Raster",
            "out.png",
            vmin="2%",
            vmax="98%",
            band_names=[1, 3, 5]
        )
        create_collage_images(
            raster,
            "Indices",
            "out.png",
            band_names=["B4", "B8", "NDVI"],
            band_titles=["Red", "NIR", "NDVI"]
        )


        # Mixed: fixed lower bound + percentile upper bound
        create_collage_images(
            raster,
            "My Raster",
            "out.png",
            vmin=0,
            vmax="99%"
        )

        # Raster collage with vector boundaries and decluttered labels
        create_collage_images(
            raster,
            "My Raster",
            "out.png",
            gdf=my_gdf,
            label_field="NAME",
            label_min_dist_px=22,
            label_max_count=80,
            label_style={"fontsize": 8}
        )
        """
        if not isinstance(raster, RioRaster):
            raster = RioRaster(raster)



        def _resolve_limit(limit: Optional[Union[float, str]], band: np.ndarray):
            if limit is None:
                return None
            if isinstance(limit, str) and limit.endswith('%'):
                try:
                    p = float(limit[:-1])
                except ValueError:
                    raise ValueError(f"Invalid percentile string for vmin/vmax: {limit!r}")
                return float(np.nanpercentile(band, p))
            return float(limit)

        def _label_points_for_geoms(gdf_: GeoDataFrame) -> np.ndarray:
            """Returns Nx2 array of label (x,y) positions in data coords."""
            geom_type = gdf_.geometry.geom_type
            use_rep = geom_type.isin(["Polygon", "MultiPolygon"]).to_numpy()
            pts = []
            for use_rp, geom in zip(use_rep, gdf_.geometry):
                if geom is None or geom.is_empty:
                    pts.append((np.nan, np.nan))
                    continue
                p = geom.representative_point() if use_rp else geom.centroid
                pts.append((p.x, p.y))
            return np.asarray(pts, dtype=float)

        def _declutter_and_draw_labels(
                ax,
                gdf_: GeoDataFrame,
                label_field_: str,
                *,
                min_dist_px: int,
                max_count: Optional[int],
                style: Dict[str, Any],
                only_largest: bool,
        ):
            if gdf_ is None or gdf_.empty:
                return
            if label_field_ not in gdf_.columns:
                raise ValueError(f"label_field={label_field_!r} not found in gdf columns.")

            gg = gdf_.loc[gdf_[label_field_].notna() & gdf_.geometry.notna()].copy()
            if gg.empty:
                return

            if only_largest:
                try:
                    gg["_area_tmp"] = gg.geometry.area
                    gg = gg.sort_values("_area_tmp", ascending=False)
                except Exception:
                    pass

            xy = _label_points_for_geoms(gg)
            labels = gg[label_field_].astype(str).to_numpy()

            disp = ax.transData.transform(xy)  # Nx2
            keep = []
            kept_disp = []

            for idx, (dxy, lab, dpos) in enumerate(zip(xy, labels, disp)):
                if not np.isfinite(dxy).all() or not np.isfinite(dpos).all():
                    continue

                if max_count is not None and len(keep) >= max_count:
                    break

                if not kept_disp:
                    keep.append((dxy[0], dxy[1], lab))
                    kept_disp.append(dpos)
                    continue

                diffs = np.asarray(kept_disp) - dpos
                dist2 = np.sum(diffs * diffs, axis=1)
                if np.min(dist2) >= (min_dist_px ** 2):
                    keep.append((dxy[0], dxy[1], lab))
                    kept_disp.append(dpos)

            for x, y, lab in keep:
                ax.text(x, y, lab, **style)

        def to_title_case(title_str: str) -> str:
            return title_str.replace("_", " ").replace("-", " ").title()

        sub_titles = [] if sub_titles is None else [to_title_case(t) for t in sub_titles]
        title = to_title_case(title)

        # --- resolve band indices to plot ---
        all_band_indices = list(range(1, raster.get_spectral_resolution() + 1))

        if band_names is None:
            bands_to_plot = all_band_indices
        else:
            bands_to_plot = []
            for b in band_names:
                if isinstance(b, int):
                    if b < 1 or b > len(all_band_indices):
                        raise ValueError(f"Invalid band index: {b}")
                    bands_to_plot.append(b)
                elif isinstance(b, str):
                    matched = False
                    for i in all_band_indices:
                        if raster.get_band_name(i) == b:
                            bands_to_plot.append(i)
                            matched = True
                            break
                    if not matched:
                        raise ValueError(f"Band name '{b}' not found in raster.")
                else:
                    raise TypeError("band_names must contain int or str values.")

        no_of_bands = len(bands_to_plot)
        no_of_cols = no_of_bands // no_of_rows if (no_of_bands % no_of_rows == 0) else int(
            np.ceil(no_of_bands / no_of_rows))

        # --- Setup Master Grid (Vertical Stack of Rows) ---
        fig = plt.figure(figsize=(5 * no_of_cols, 4 * no_of_rows), constrained_layout=True)
        gs = gridspec.GridSpec(no_of_rows, 1, figure=fig)

        raster_crs = raster.get_crs()
        transform = raster.get_geo_transform()
        nodata_value = raster.get_nodata_value()

        if gdf is not None and not gdf.empty and getattr(gdf, "crs", None) is not None:
            if gdf.crs != raster_crs:
                gdf = gdf.to_crs(raster_crs, inplace=False)

        cmap_obj = plt.get_cmap(cmap).copy()
        cmap_obj.set_bad(color='white')

        default_label_style = dict(
            fontsize=9,
            color="black",
            ha="center",
            va="center",
            zorder=10,
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.65),
        )
        if label_style:
            default_label_style.update(label_style)

        # --- Plotting Loop ---
        for plot_idx, band_idx in enumerate(bands_to_plot):
            band = raster.get_data_array(band_idx).astype(float)

            # if nodata_value is not None:
            #     band[band == nodata_value] = np.nan

            # --- Locate this block inside create_collage_images plotting loop ---
            if nodata_value is not None:
                if np.isnan(nodata_value):
                    band[np.isnan(band)] = np.nan
                else:
                    band[band == nodata_value] = np.nan

            h, w = band.shape
            extent = RioProcess._extent_from_transform(transform, w, h)

            vmin_resolved = _resolve_limit(vmin, band)
            vmax_resolved = _resolve_limit(vmax, band)

            row_idx = plot_idx // no_of_cols
            col_idx = plot_idx % no_of_cols

            # Calculate exactly how many bands are allocated to this current row
            bands_in_this_row = min(no_of_cols, no_of_bands - (row_idx * no_of_cols))

            # Dynamic Sub-gridding: builds a custom row grid depending on the element count
            if bands_in_this_row == 1:
                # Centered layout: build a single-column row grid inside this row slot
                row_gs = gridspec.GridSpecFromSubplotSpec(1, 1, subplot_spec=gs[row_idx])
                ax = fig.add_subplot(row_gs[0, 0])
            else:
                # Standard sequential layout
                row_gs = gridspec.GridSpecFromSubplotSpec(1, no_of_cols, subplot_spec=gs[row_idx])
                ax = fig.add_subplot(row_gs[0, col_idx])

            divider = make_axes_locatable(ax)
            cax = divider.append_axes("right", size="5%", pad=0.1)

            im = ax.imshow(
                band,
                cmap=cmap_obj,
                interpolation='nearest',
                extent=extent,
                origin='upper',
                vmin=vmin_resolved,
                vmax=vmax_resolved,
            )

            if gdf is not None and not gdf.empty:
                gdf.boundary.plot(ax=ax, edgecolor=edgecolor, linewidth=linewidth)

                if label_field:
                    _declutter_and_draw_labels(
                        ax,
                        gdf,
                        label_field,
                        min_dist_px=label_min_dist_px,
                        max_count=label_max_count,
                        style=default_label_style,
                        only_largest=label_only_largest,
                    )

            # if band_titles is not None:
            #     band_titles = list(band_titles)
            # elif sub_titles is not None:
            #     band_titles = list(sub_titles)
            # else:
            #     band_titles = []
            #
            # band_title = (
            #     band_titles[plot_idx]
            #     if band_titles and plot_idx < len(band_titles)
            #     else raster.get_band_name(band_idx)
            # )
            #
            # ax.set_title(f"{title} – {band_title}" if title else band_title)

            # 1. Consolidate the source list (Fallback chain)
            titles_source = band_titles if band_titles is not None else (sub_titles or [])
            titles_list = list(titles_source)

            # 2. Extract the specific band title safely, falling back to raster name if out of bounds
            if titles_list and plot_idx < len(titles_list):
                band_title = titles_list[plot_idx]
            elif band_titles is not None:
                band_title = raster.get_band_name(band_idx)
            else:
                band_title = None

            # 3. Construct the final title based on what exists
            if title and band_title:
                ax.set_title(f"{title} – {band_title}")
            elif title:
                ax.set_title(title)
            elif band_title:
                ax.set_title(band_title)

            # 4. Hide axes
            ax.set_xticks([])
            ax.set_yticks([])

            fig.colorbar(im, cax=cax)

        # Note: Empty tracking grid slots from GridSpec don't build invisible placeholder axes,
        # so we don't need a secondary hide loop.
        fig.savefig(output_fp, dpi=150, bbox_inches='tight')
        plt.close(fig)
        return output_fp



    @staticmethod
    def stack_rasters_in_memory(
            sources: Sequence[Union['RioRaster', np.ndarray]],
            reference: 'RioRaster' = None,
            band_names: List[str] = None,
            resampling: str = "nearest"
    ) -> 'RioRaster':
        """
        Align and stack sources into a new multiband in-memory RioRaster.

        If reference is None, the first source must be a RioRaster and is used
        as the grid/CRS reference template.
        """
        if not sources:
            raise ValueError("No sources provided.")

        # 1. Establish the reference RioRaster template
        if reference is not None:
            ref_r = reference
        else:
            first_item = sources[0]
            if not isinstance(first_item, RioRaster):
                raise ValueError(
                    "First source is a numpy array. Please provide a RioRaster "
                    "reference or make the first source a RioRaster."
                )
            ref_r = first_item

        arrays = []

        # 2. Iterate and seamlessly align grids using make_coincident
        for idx, source in enumerate(sources):
            # Convert pure numpy array inputs to a transient RioRaster object
            if not isinstance(source, RioRaster):
                arr = np.asarray(source)
                if arr.ndim not in (2, 3):
                    raise ValueError(f"Array source must be 2D or 3D. Got shape: {arr.shape}")
                r = RioRaster.raster_from_array(
                    img_arr=arr,
                    crs=ref_r.get_crs(),
                    g_transform=ref_r.get_geo_transform(),
                    nodata_value=ref_r.get_nodata_value()
                )
            else:
                r = source

            # --- THE REFACTOR ---
            # Instead of manual padding/alignment subfunctions, force alignment to the reference template.
            # If r is already ref_r, we bypass it to save memory/cycles.
            if r is not ref_r:
                r = r.make_coincident(template=ref_r, resampling=resampling, in_place=False)

            # Extract standard 3D array layout from aligned RioRaster
            arr_data = r.get_data_array()
            if arr_data.ndim == 2:
                arr_data = arr_data[np.newaxis, ...]
            elif arr_data.ndim != 3:
                raise ValueError(f"Invalid raster data shape at index {idx}: {arr_data.shape}")

            arrays.append(arr_data)

        # 3. Concatenate arrays along the band axis
        arrays = [np.asarray(a, dtype=np.float32) for a in arrays]
        stacked = np.concatenate(arrays, axis=0)
        total_bands = stacked.shape[0]

        if band_names is not None and len(band_names) != total_bands:
            raise ValueError(
                f"band_names count ({len(band_names)}) does not match "
                f"stacked band count ({total_bands})."
            )

        # 4. Construct updated metadata structure
        meta = ref_r.get_meta().copy()
        ref_nodata = ref_r.get_nodata_value()
        nodata = ref_nodata if ref_nodata is not None else np.nan

        meta.update({
            "count": total_bands,
            "dtype": "float32",
            "height": stacked.shape[1],
            "width": stacked.shape[2],
            "transform": ref_r.get_geo_transform(),
            "crs": ref_r.get_crs(),
            "nodata": nodata,
        })

        ds_reader = RioRaster.rio_dataset_from_array(
            stacked,
            meta,
            band_names or []
        )

        return RioRaster(ds_reader)



    @staticmethod
    def view_raster_on_folium(
        raster,
        tiles: str = "OpenStreetMap",
        gdf: GeoDataFrame = None,
        show_legend: bool = True,
        legend_title: str = "Raster Values",
        colormap="viridis",
        vmin=None,
        vmax=None,
        opacity: float = 0.7,
        output_html: str = None,
    ):
        """
        Display a single-band raster on an interactive Folium web map.

        Parameters
        ----------
        raster : RioRaster
            Raster object containing at least one band.
        tiles : str, optional (default="OpenStreetMap")
            Basemap tile provider used in the Folium map.

            Supported built-in tile names include:
                - "OpenStreetMap"                  → Standard OSM basemap
                - "Stamen Terrain"                 → Terrain / relief style
                - "Stamen Toner"                   → High-contrast B&W map
                - "Stamen Watercolor"              → Artistic watercolor map
                - "CartoDB positron"               → Clean light basemap
                - "CartoDB dark_matter"            → Dark background basemap
                - "Wikimedia"                      → Wikimedia OSM tiles

            You may also use **custom XYZ tile providers** using `folium.TileLayer`, e.g.:

            - Google Maps (roadmap)
              URL: "https://mt1.google.com/vt/lyrs=r&x={x}&y={y}&z={z}"

            - Google Satellite
              URL: "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"

            - Esri World Imagery (satellite)
              URL: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

            - Esri Topographic
              URL: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"

            Notes
            -----
            • XYZ tile URLs must include placeholders: {x}, {y}, {z}.
            • Google and ESRI tiles may impose use restrictions.
            • Tile choice does not affect raster computation — only the visual background.

        gdf : GeoDataFrame, optional
            A vector boundary (single GeoDataFrame) to overlay on the map.
        show_legend : bool, default=True
            Whether to display a dynamic legend matching raster colormap.
        legend_title : str, optional
        colormap : str or callable, default="viridis"
            Name of matplotlib colormap.
        vmin, vmax : float, optional
            Color range limits. If None → computed from raster.
        opacity : float, default=0.7
            Opacity of raster overlay.
        output_html : str, optional
            If provided, the map is saved to an HTML file.

        Returns
        -------
        folium.Map
            Folium map object with raster overlay.

        Usage:
            r = RioRaster("tests/data/terrain/merit_dem.tif")
            aoi_gdf = your_aoi_gdf   # e.g. catchment boundary, villages, etc.

            m = RioProcess.view_raster_on_folium(
                raster=r,
                band=1,
                cmap="terrain",
                opacity=0.8,
                show_legend=True,
                legend_label="Elevation (m)",
                gdf=aoi_gdf,
                edgecolor="red",
                linewidth=1.0,
            )

            m.save("dem_with_aoi.html")

        """


        # ---------------------------------------------
        # Read raster data
        # ---------------------------------------------
        arr = raster.get_data_array(1).astype(float)
        nodata = raster.get_nodata_value()
        arr[arr == nodata] = np.nan

        vmin = np.nanmin(arr) if vmin is None else vmin
        vmax = np.nanmax(arr) if vmax is None else vmax

        cmap = cm.get_cmap(colormap)
        norm = colors.Normalize(vmin=vmin, vmax=vmax)

        rgba_img = cmap(norm(arr))
        rgba_img = (rgba_img * 255).astype("uint8")

        # encode PNG
        buf = io.BytesIO()
        plt_img = rasterio.plot.show
        import matplotlib.pyplot as plt

        plt.imsave(buf, rgba_img, format="png")
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode("utf-8")

        # bounding box
        transform = raster.get_geo_transform()
        h, w = arr.shape
        xmin = transform.c
        ymax = transform.f
        xmax = xmin + transform.a * w
        ymin = ymax + transform.e * h

        bounds = [[ymin, xmin], [ymax, xmax]]

        # ---------------------------------------------
        # Create Folium map centered on raster
        # ---------------------------------------------
        center_lat = (ymin + ymax) / 2
        center_lon = (xmin + xmax) / 2

        m = folium.Map(location=[center_lat, center_lon], zoom_start=10, tiles=tiles)

        # ---------------------------------------------
        # Add raster overlay
        # ---------------------------------------------
        folium.raster_layers.ImageOverlay(
            image="data:image/png;base64," + img_b64,
            bounds=bounds,
            opacity=opacity,
            name="Raster",
        ).add_to(m)

        # ---------------------------------------------
        # Add vector overlay if provided
        # ---------------------------------------------
        if gdf is not None and not gdf.empty:
            if gdf.crs != raster.get_crs():
                gdf = gdf.to_crs(raster.get_crs())

            folium.GeoJson(
                gdf,
                name="Vector Overlay",
                style_function=lambda x: {
                    "color": "green",
                    "weight": 2,
                    "fillOpacity": 0,
                },
            ).add_to(m)

        # ---------------------------------------------
        # Add legend
        # ---------------------------------------------
        if show_legend:
            gradient = "".join(
                [
                    f'<div style="background: rgb{tuple(int(c*255) for c in cmap(i/100)[:3])}; width: 100%; height: 3px"></div>'
                    for i in range(100)
                ]
            )

            legend_html = f"""
            <div style="
                position: fixed;
                bottom: 20px;
                right: 20px;
                background: white;
                padding: 10px;
                border: 2px solid grey;
                z-index: 999999;
                font-size: 12px;
            ">
                <b>{legend_title}</b><br>
                {gradient}
                <div style="display:flex; justify-content:space-between;">
                    <span>{vmin:.2f}</span>
                    <span>{vmax:.2f}</span>
                </div>
            </div>
            """
            m.get_root().html.add_child(folium.Element(legend_html))

        folium.LayerControl().add_to(m)

        if output_html:
            m.save(output_html)

        return m

    @staticmethod
    def open_folium_in_browser(folium_map):
        """
        Open a Folium map in the default web browser without manually saving a file.
        Usage:
            m = RioProcess.view_raster_on_folium(raster)
            open_folium_in_browser(m)
        """
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
        folium_map.save(tmp.name)
        webbrowser.open(tmp.name)


    @staticmethod
    def zonal_stats_by_band(
            raster: RioRaster,
            zones_gdf: gpd.GeoDataFrame,
            *,
            zone_id_field: str,
            stats: Sequence[str] = ("mean", "median", "std", "min", "max", "cv"),
            all_touched: bool = True,
            out_xlsx: Optional[str | Path] = None,
            sheet_name_prefix: str = "",
            return_format: Literal["dict", "dataframe"] = "dict",
    ) -> Dict[str, pd.DataFrame] | pd.DataFrame:
        """
        Compute zonal statistics by raster band.

        return_format="dict":
            returns {band_name: dataframe}

        return_format="dataframe":
            returns one dataframe with a Layer column.
        """

        ds = raster.get_dataset()
        if ds is None:
            raise ValueError("Raster dataset is not open.")

        gdf = zones_gdf.copy()
        if gdf.crs != ds.crs:
            gdf = gdf.to_crs(ds.crs)

        band_names = list(ds.descriptions)
        if not band_names or all(b is None for b in band_names):
            band_names = [f"band{i}" for i in range(1, ds.count + 1)]

        stat_set = set(stats)
        band_results: Dict[str, list] = {b: [] for b in band_names}

        def compute(arr: np.ndarray) -> dict:
            x = arr[np.isfinite(arr)]
            if x.size == 0:
                return {s: np.nan for s in stat_set}

            out = {}
            mu = np.mean(x)

            if "mean" in stat_set:
                out["mean"] = float(mu)
            if "median" in stat_set:
                out["median"] = float(np.median(x))
            if "min" in stat_set:
                out["min"] = float(np.min(x))
            if "max" in stat_set:
                out["max"] = float(np.max(x))
            if "std" in stat_set or "cv" in stat_set:
                std = float(np.std(x))
                if "std" in stat_set:
                    out["std"] = std
                if "cv" in stat_set:
                    out["cv"] = float(std / mu) if mu != 0 else np.nan

            return out

        for _, row in gdf.iterrows():
            geom = row.geometry
            zone_id = row[zone_id_field]

            out_img, _ = mask(
                ds,
                [geom],
                crop=True,
                all_touched=all_touched,
                nodata=ds.nodata,
                filled=True,
            )

            for i, band in enumerate(band_names):
                band_arr = out_img[i].astype("float32")

                if ds.nodata is not None:
                    band_arr[band_arr == ds.nodata] = np.nan

                stats_dict = compute(band_arr.ravel())
                stats_dict[zone_id_field] = zone_id
                stats_dict["Layer"] = band

                band_results[band].append(stats_dict)

        band_dfs: Dict[str, pd.DataFrame] = {}

        for band, rows in band_results.items():
            df = pd.DataFrame(rows)
            cols = ["Layer", zone_id_field] + [s for s in stats if s in df.columns]
            band_dfs[band] = df[cols]

        combined_df = pd.concat(
            band_dfs.values(),
            ignore_index=True,
        )

        if out_xlsx:
            out_xlsx = Path(out_xlsx)
            out_xlsx.parent.mkdir(parents=True, exist_ok=True)

            with pd.ExcelWriter(out_xlsx, engine="openpyxl", mode="w") as writer:
                combined_df.to_excel(
                    writer,
                    sheet_name=f"{sheet_name_prefix}all_bands"[:31],
                    index=False,
                )

                for band, df in band_dfs.items():
                    sheet = f"{sheet_name_prefix}{band}"[:31]
                    df.to_excel(writer, sheet_name=sheet, index=False)

        if return_format == "dict":
            return band_dfs

        return combined_df

    @staticmethod
    def zonal_stats_by_lookup_table(
            raster: RioRaster,
            zones_gdf: gpd.GeoDataFrame,
            *,
            zone_id_field: str,
            lookup_table: Union[pd.DataFrame, Dict[int, str]],
            value_col: str = "pixel_value",
            label_col: str = "class_name",
            all_touched: bool = True,
            area_units: Literal["pixels", "meters", "kilometers"] = "kilometers",
            out_xlsx: Optional[Union[str, Path]] = None,
    ) -> pd.DataFrame:
        """
        Compute zonal statistics for categorical rasters based on a class lookup table.
        Calculates total pixel counts and geometric area breakdowns per class for each zone.
        """
        ds = raster.get_dataset()
        if ds is None:
            raise ValueError("Raster dataset is not open.")

        # Align CRS
        gdf = zones_gdf.copy()
        if gdf.crs != ds.crs:
            gdf = gdf.to_crs(ds.crs)

        # Normalize Lookup Table to Dict[int, str]
        if isinstance(lookup_table, pd.DataFrame):
            lut_dict = dict(zip(lookup_table[value_col].astype(int), lookup_table[label_col].astype(str)))
        else:
            lut_dict = {int(k): str(v) for k, v in lookup_table.items()}

        nodata_val = raster.get_nodata_value()

        # Calculate spatial pixel resolution area matrix
        x_res, y_res = raster.get_spatial_resolution(in_meter=True)
        pixel_area_m2 = x_res * y_res

        rows = []

        # Process each zone geometry matrix
        for _, zone_row in gdf.iterrows():
            zone_id = zone_row[zone_id_field]
            geom = zone_row.geometry

            if geom is None or geom.is_empty:
                continue

            # Mask raster array to current zone geometry
            out_img, _ = mask(
                ds,
                [geom],
                crop=True,
                all_touched=all_touched,
                nodata=ds.nodata,
                filled=True,
            )

            # Extract 2D single-band cluster
            band_arr = out_img[0].ravel()

            # Filter out Nodata allocations
            if nodata_val is not None:
                band_arr = band_arr[band_arr != nodata_val]

            if band_arr.size == 0:
                continue

            # Get unique counts inside zone
            vals, counts = np.unique(band_arr, return_counts=True)

            # ✅ INDENTED INSIDE THE ZONE LOOP:
            # Process and append results for the current governorate immediately
            for val, count in zip(vals, counts):
                val_int = int(val)
                class_name = lut_dict.get(val_int, f"Unknown Class {val_int}")

                # Calculate spatial footprint size
                if area_units == "pixels":
                    area_val = float(count)
                elif area_units == "meters":
                    area_val = float(count * pixel_area_m2)
                else:  # kilometers
                    area_val = float((count * pixel_area_m2) / 1_000_000.0)

                rows.append({
                    "Zone": zone_id,
                    "Class_Value": val_int,
                    "Class_Name": class_name,
                    "Count": int(count),
                    "Area": round(area_val, 4)
                })

        # --- Outside the main iterator loop entirely ---
        df_long = pd.DataFrame(rows)

        # Optional clean output to Excel target
        if out_xlsx and not df_long.empty:
            out_xlsx = Path(out_xlsx)
            out_xlsx.parent.mkdir(parents=True, exist_ok=True)
            df_long.to_excel(out_xlsx, index=False)

        return df_long

    @staticmethod
    def generate_zonal_matrix(
            df_long: pd.DataFrame,
            value_column: Literal["Count", "Area", "percentage"] = "Area",
            fill_value: float = 0.0,
            round_digits: int = 2,
            log_summary: bool = True,
            normalize: Literal["row", "column", "grand"] = "row",
            matrix_title: str = "Zonal Composition Matrix"
    ) -> pd.DataFrame:
        """
        Transforms long-form categorical zonal statistics into a cross-tabulated wide matrix.
        Supports Counts, Absolute Areas, or normalized Zone-wise percentages.

        Output format layout for "percentage":
            Zone       | Class_A_Name | Class_B_Name | Class_C_Name | Total
            -----------+--------------+--------------+--------------+-------
            Catchment1 |     12.40    |      0.00    |     87.60    | 100.0
            Catchment2 |     50.00    |     25.00    |     25.00    | 100.0

        Parameters
        ----------
        df_long : pd.DataFrame
            The result output coming directly from `zonal_stats_by_lookup_table`.
        value_column : 'Count' | 'Area' | 'percentage', default='Area'
            Determine which numerical metric populates the intersection cells.
        fill_value : float, default=0.0
            Fallback value for zero-presence combinations.
        round_digits : int, default=2
            Decimal rounding precision.
        log_summary : bool, default=True
            If true, fires execution output seamlessly across console logs using `da_logger`.
        normalize: Literal['row', 'column', 'grand'] = 'row'
        matrix_title : str
            Title header parsed to logger wrapper.

        Usage:
            # Create lookup configuration layout
            lookup_df = pd.DataFrame({
                "pixel_value": [10, 20, 30, 40],
                "class_name": ["Trees", "Shrubland", "Grassland", "Cropland"],
                "color": ["#006400", "#ffbb22", "#ffff4c", "#f00082"]
            }) or
            lookup_df = my_raster_object.get_lookup_table()

            # 1. Run long-form intersection computation
            df_breakdown = RioProcess.zonal_stats_by_lookup_table(
                raster=my_raster_object,
                zones_gdf=catchments_gdf,
                zone_id_field="catchment_id",
                lookup_table=lookup_df,
                area_units="kilometers"  # Converts spatial pixels to Sq. Kilometers
            )

            # 2. Reshape immediately into a wide cross-tabulation matrix
            matrix_summary = RioProcess.generate_zonal_matrix(
                df_long=df_breakdown,
                value_column="Area",      # Pulls metric from the Area column calculations
                matrix_title="Catchment Land Cover Distribution (Sq. km)"
            )
        """
        if df_long.empty:
            da_logger.warning("Empty dataframe provided. Cannot generate matrix.")
            return pd.DataFrame()

        # 1. Pivot layout based on requested base metric
        pivot_metric = "Count" if value_column == "percentage" else value_column

        matrix_df = df_long.pivot(
            index="Zone",
            columns="Class_Name",
            values=pivot_metric
        ).fillna(fill_value)

        # 2. Normalize rows to percentage scale if requested
        # if value_column == "percentage":
        #     row_totals = matrix_df.sum(axis=1)
        #     # Safe divide to handle any edge cases where zone total might be zero
        #     matrix_df = matrix_df.div(row_totals, axis=0).replace([np.inf, -np.inf], 0).fillna(0) * 100.0
        if value_column == "percentage":
            if normalize == "row":
                totals = matrix_df.sum(axis=1)
                matrix_df = matrix_df.div(totals, axis=0)

            elif normalize == "column":
                totals = matrix_df.sum(axis=0)
                matrix_df = matrix_df.div(totals, axis=1)

            elif normalize == "grand":
                total = matrix_df.values.sum()
                matrix_df = matrix_df / total

            else:
                raise ValueError("normalize must be 'row', 'column', or 'grand'.")

            matrix_df = matrix_df.replace([np.inf, -np.inf], 0).fillna(0) * 100.0
        # 3. Append horizontal aggregation summary metrics and round figures
        matrix_df = matrix_df.round(round_digits)
        matrix_df["Total"] = matrix_df.sum(axis=1).round(round_digits)
        matrix_df = matrix_df.reset_index()

        if log_summary:
            table_text = matrix_df.to_string(index=False)
            da_logger.success(
                matrix_title,
                f"Values based on: {value_column}\n\n{table_text}",
                as_table=True,
                tablefmt="grid"
            )

        return matrix_df



    @staticmethod
    def zonal_stats_to_table_dataframe(
            zonal_stats: Dict[str, pd.DataFrame],
            zone_col: str,
            stats: Sequence[str] = ("mean", "median", "max", "cv"),
            round_digits: int = 2,
            sort_zones: bool = True,
    ) -> pd.DataFrame:
        """
        Convert band-wise zonal statistics into a wide table.

        Output format:
            Layer | Statistics | Zone 1 | Zone 2 | Zone 3 ...
        """

        rows = []

        all_zones = set()

        for df in zonal_stats.values():
            if zone_col not in df.columns:
                raise ValueError(
                    f"'{zone_col}' not found in dataframe columns: {df.columns.tolist()}"
                )

            all_zones.update(
                df[zone_col].dropna().astype(str).tolist()
            )

        all_zones = sorted(all_zones) if sort_zones else list(all_zones)

        stat_labels = {
            "mean": "Mean",
            "median": "Median",
            "min": "Min",
            "max": "Max",
            "std": "Std",
            "cv": "CV",
        }

        for layer_name, df in zonal_stats.items():
            df = df.copy()

            for col in stats:
                if col in df.columns:
                    df[col] = pd.to_numeric(
                        df[col],
                        errors="coerce",
                    ).round(round_digits)

            for stat_col in stats:
                if stat_col not in df.columns:
                    continue

                row = {
                    "Layer": layer_name,
                    "Statistics": stat_labels.get(
                        stat_col,
                        stat_col.title(),
                    ),
                }

                for zone_name in all_zones:
                    value = df.loc[
                        df[zone_col].astype(str) == str(zone_name),
                        stat_col,
                    ]

                    row[zone_name] = (
                        value.iloc[0]
                        if not value.empty else "-"
                    )

                rows.append(row)

        return pd.DataFrame(rows).fillna("-")

    @staticmethod
    def log_zonal_stats_table(
            zonal_stats: Dict[str, pd.DataFrame],
            zone_col: str,
            stats: Sequence[str] = ("mean", "median", "max", "cv"),
            round_digits: int = 2,
            title: str = "Zonal Statistics",
    ) -> pd.DataFrame:
        """
        Log zonal statistics using da_logger.success and return the table DataFrame.
        """

        final_df = RioProcess.zonal_stats_to_table_dataframe(
            zonal_stats=zonal_stats,
            zone_col=zone_col,
            stats=stats,
            round_digits=round_digits,
        )

        table_text = final_df.to_string(index=False)

        da_logger.success(title, table_text, as_table=True, tablefmt="grid")

        return final_df



    @staticmethod
    def two_raster_cross_tabulation(
            raster_a: RioRaster,
            raster_b: RioRaster,
            lut_a: Union[pd.DataFrame, Dict[int, str]],
            lut_b: Union[pd.DataFrame, Dict[int, str]],
            *,
            label_a: str = "Raster_A",
            label_b: str = "Raster_B",
            area_units: Literal["pixels", "kilometers", "percentage"] = "kilometers",
            normalization_mode: Literal["row", "column", "grand"] = "row",
            out_xlsx: Optional[Union[str, Path]] = None
    ) -> pd.DataFrame:
        """
        Computes a cross-tabulation matrix between two categorical rasters.
        Supports Counts, Absolute Areas (Sq. Km), or Percentages normalized by
        Row, Column, or Grand Total.
        """
        new_raster_a = raster_a.make_coincident(raster_b, in_place=False)

        # 1. Read raw arrays
        new_raster_a = raster_a.make_coincident(raster_b, in_place=False)

        arr_a = new_raster_a.get_data_array(1, convert_no_data_2_nan=True)
        arr_b = raster_b.get_data_array(1, convert_no_data_2_nan=True)

        if arr_a.shape != arr_b.shape:
            raise ValueError(
                f"Raster shapes do not match: {arr_a.shape} vs {arr_b.shape}."
            )

        flat_a = arr_a.ravel()
        flat_b = arr_b.ravel()

        valid_mask = (~np.isnan(flat_a)) & (~np.isnan(flat_b))

        nodata_a = new_raster_a.get_nodata_value()
        nodata_b = raster_b.get_nodata_value()

        if nodata_a is not None:
            valid_mask &= flat_a != nodata_a

        if nodata_b is not None:
            valid_mask &= flat_b != nodata_b

        clean_a = flat_a[valid_mask].astype(int)
        clean_b = flat_b[valid_mask].astype(int)

        # 2. Normalize Lookup Tables to Dicts
        if isinstance(lut_a, pd.DataFrame):
            dict_a = dict(zip(lut_a["pixel_value"].astype(int), lut_a["class_name"].astype(str)))
        else:
            dict_a = {int(k): str(v) for k, v in lut_a.items()}

        if isinstance(lut_b, pd.DataFrame):
            dict_b = dict(zip(lut_b["pixel_value"].astype(int), lut_b["class_name"].astype(str)))
        else:
            dict_b = {int(k): str(v) for k, v in lut_b.items()}

        # # 3. Flatten arrays for pixel-to-pixel pairing
        # flat_a = arr_a.ravel()
        # flat_b = arr_b.ravel()

        # 3. Filter out NoData and NaN values from BOTH rasters
        # valid_mask = (
        #         (flat_a != raster_a.get_nodata_value()) & (~np.isnan(flat_a)) &
        #         (flat_b != raster_b.get_nodata_value()) & (~np.isnan(flat_b))
        # )
        # clean_a = flat_a[valid_mask].astype(int)
        # clean_b = flat_b[valid_mask].astype(int)

        if clean_a.size == 0:
            return pd.DataFrame()

        # 5. Generate base matrix using Pandas Crosstab (Pixel Counts)
        df_matrix = pd.crosstab(
            index=clean_a,
            columns=clean_b,
            rownames=[label_a],
            colnames=[label_b]
        )

        # Default fallback sheet name
        sheet_name = "Zonal_Stats"

        # 6. Apply Area Units Scale Transformation / Normalization Matrix
        if area_units == "kilometers":
            x_res, y_res = raster_a.get_spatial_resolution(in_meter=True)
            pixel_area_km2 = (x_res * y_res) / 1_000_000.0
            df_matrix = (df_matrix * pixel_area_km2).round(2)

            # Standard Margins for absolute physical values
            df_matrix["Total"] = df_matrix.sum(axis=1).round(2)
            df_matrix.loc["Total"] = df_matrix.sum(axis=0).round(2)
            sheet_name = "Area_Km2"

        elif area_units == "percentage":
            if normalization_mode == "row":
                # QUERY INFO: Answers "Where does all of Raster A's class footprint sit across Raster B's environments?"
                # Example: What % of the Oasis archetype sits on top of Alluvial vs Bedrock Aquifers?
                row_totals = df_matrix.sum(axis=1)
                df_matrix = df_matrix.div(row_totals, axis=0).fillna(0) * 100.0
                df_matrix = df_matrix.round(2)
                df_matrix["Total"] = 100.0
                sheet_name = "Row_Percentage"

            elif normalization_mode == "column":
                # QUERY INFO: Answers "Of all the space occupied by a column environment, what % falls into each row class?"
                # Example: Out of 100% of the Alluvial Aquifer area, how much of it is occupied by Oasis vs Wadi vs Desert?
                col_totals = df_matrix.sum(axis=0)
                df_matrix = df_matrix.div(col_totals, axis=1).fillna(0) * 100.0
                df_matrix = df_matrix.round(2)
                df_matrix.loc["Total"] = 100.0
                sheet_name = "Column_Percentage"

            elif normalization_mode == "grand":
                # QUERY INFO: Answers "What percentage of the entire regional landscape consists of this specific intersection?"
                # Example: What % of the total Al Madinah province is specifically Oasis overlaying an Alluvial Aquifer?
                grand_total = df_matrix.to_numpy().sum()
                df_matrix = (df_matrix / grand_total) * 100.0
                df_matrix = df_matrix.round(2)

                df_matrix["Total"] = df_matrix.sum(axis=1).round(2)
                df_matrix.loc["Total"] = df_matrix.sum(axis=0).round(2)
                sheet_name = "Grand_Percentage"

        else:  # pixels
            df_matrix["Total"] = df_matrix.sum(axis=1)
            df_matrix.loc["Total"] = df_matrix.sum(axis=0)
            sheet_name = "Pixel_Counts"

        # 7. Map raw integer pixel values to actual Class Names
        df_matrix.index = df_matrix.index.map(lambda x: dict_a.get(int(x), f"Unknown_{x}") if x != "Total" else "Total")
        df_matrix.columns = df_matrix.columns.map(
            lambda x: dict_b.get(int(x), f"Unknown_{x}") if x != "Total" else "Total")
        df_matrix = df_matrix.reset_index()

        # 8. Safe Append to Excel target layout
        if out_xlsx:
            out_xlsx = Path(out_xlsx)
            out_xlsx.parent.mkdir(parents=True, exist_ok=True)

            # Check if the Excel file already exists to trigger append mode safely
            if out_xlsx.exists():
                try:
                    with pd.ExcelWriter(out_xlsx, engine="openpyxl", mode="a", if_sheet_exists="replace") as writer:
                        df_matrix.to_excel(writer, sheet_name=sheet_name, index=False)
                except Exception as e:
                    # Fallback if file is corrupted/locked but exists
                    df_matrix.to_excel(out_xlsx, sheet_name=sheet_name, index=False)
            else:
                # Create fresh file if it doesn't exist
                with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
                    df_matrix.to_excel(writer, sheet_name=sheet_name, index=False)

        return df_matrix