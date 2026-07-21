from __future__ import annotations
import os
from pathlib import Path
from typing import Union, Tuple, Optional, Literal, List

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import matplotlib.pyplot as plt

from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger


class GpdVector:
    """
    VectorClass wrapper for GeoPandas GeoDataFrames.
    Provides high-level spatial analysis and visualization tools.
    """

    def __init__(self, src: Optional[Union[str, Path, gpd.GeoDataFrame]] = None):
        self.df: gpd.GeoDataFrame = gpd.GeoDataFrame()
        if src is not None:
            self.set_data(src)

    def __repr__(self):
        if self.empty:
            return "<GpdVector: Empty>"
        return f"<GpdVector: {len(self.df)} features, CRS: {self.df.crs}>"

    def set_data(self, src: Union[str, Path, gpd.GeoDataFrame]):
        """Load data from a file path or an existing GeoDataFrame."""
        try:
            if isinstance(src, (str, Path)):
                if os.path.exists(src):
                    self.df = gpd.read_file(src)
                else:
                    raise FileNotFoundError(f"Vector file not found at {src}")
            elif isinstance(src, gpd.GeoDataFrame):
                self.df = src
            else:
                raise TypeError("Source must be a file path or a GeoDataFrame.")
        except Exception as e:
            da_logger.error(f"Error loading vector data: {e}")

    @property
    def empty(self) -> bool:
        return self.df is None or self.df.empty

    def to_crs(self, crs: Union[str, int, dict], in_place: bool = True) -> 'GpdVector':
        """Reproject the vector data to a new CRS."""
        new_df = self.df.to_crs(crs)
        if in_place:
            self.df = new_df
            return self
        return GpdVector(new_df)

    def find_location_of(
            self,
            raster: 'RioRaster',
            stats_value: Literal["min", "max"] = "min",
            window_size: Tuple[int, int] = (3, 3),
            band: int = 1
    ) -> gpd.GeoDataFrame:
        if self.empty:
            raise ValueError("GpdVector is empty.")

        raster_crs = raster.get_crs()
        vector_df = self.df if self.df.crs == raster_crs else self.df.to_crs(raster_crs)
        results = []

        for idx, row in vector_df.iterrows():
            try:
                temp_gdf = gpd.GeoDataFrame([row], crs=vector_df.crs)
                clipped = raster.clip_raster(temp_gdf, in_place=False)

                point_found = False
                if clipped and not clipped.empty:
                    data = clipped.get_data_array(band=band, convert_no_data_2_nan=True)

                    # Call the updated safe method
                    loc = BandProcess.find_local_extreme(data, stats_value, window_size)

                    if loc is not None:
                        row_idx, col_idx = loc
                        affine = clipped.get_geo_transform()
                        x, y = affine * (col_idx + 0.5, row_idx + 0.5)
                        pixel_val = clipped.get_pixel_value(x, y, band=band)
                        point_found = True

                # FALLBACK: If standard or safe method failed, use Centroid
                if not point_found:
                    da_logger.warning(f"Index {idx}: All-NaN or Clip failure. Using Centroid fallback.")
                    centroid = row.geometry.centroid
                    x, y = centroid.x, centroid.y
                    pixel_val = raster.get_pixel_value(x, y, band=band)

                new_record = row.copy()
                new_record['geometry'] = shapely.geometry.Point(x, y)
                new_record['pixel_val'] = pixel_val
                results.append(new_record)

            except Exception as e:
                da_logger.error(f"Critical failure at index {idx}: {e}")

        return gpd.GeoDataFrame(results, crs=vector_df.crs)

    def quick_layout(
            self,
            column: Optional[str] = None,
            cmap: str = "viridis",
            title: str = "Vector Layout",
            figsize: Tuple[int, int] = (10, 10),
            legend: bool = True,
            edgecolor: str = "black",
            linewidth: float = 0.5,
            alpha: float = 0.8,
            ax: Optional[plt.Axes] = None,
            **kwargs
    ) -> plt.Axes:
        """
        Creates a quick thematic map layout.

        :param column: Attribute column for choropleth symbology.
        :param cmap: Matplotlib colormap.
        :param ax: Existing axis to plot on (useful for layering points over polygons).
        """
        if self.empty:
            da_logger.warning("GpdVector is empty. Nothing to plot.")
            return ax

        plot_ax = self.df.plot(
            column=column,
            cmap=cmap if column else None,
            legend=legend if column else False,
            figsize=figsize if ax is None else None,
            edgecolor=edgecolor,
            linewidth=linewidth,
            alpha=alpha,
            ax=ax,
            **kwargs
        )

        plot_ax.set_title(title)
        plot_ax.set_axis_off()

        return plot_ax

    def save(self, file_path: Union[str, Path], driver: str = "ESRI Shapefile"):
        """Save the GeoDataFrame to a file."""
        self.df.to_file(file_path, driver=driver)

    def get_gdf(self) -> gpd.GeoDataFrame:
        """Return the underlying GeoDataFrame."""
        return self.df