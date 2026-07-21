import digitalarzengine.proj_bootstrap
from pathlib import Path

import pyproj
from geopandas import GeoDataFrame
from matplotlib import pyplot as plt

import math
import os

import pandas as pd
import rasterio
from osgeo import gdal
import geopandas as gpd

import numpy as np
import rasterio as rio
from typing import Union, List, Optional, Tuple, Literal, Sequence, Dict, Any

import shapely
from affine import Affine
from rasterio import CRS, windows

from rasterio.mask import mask
from rasterio.transform import from_bounds, rowcol

from shapely import box
from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger

ResampleMethod = Literal[
    "nearest", "bilinear", "cubic", "average", "mode",
    "max", "min", "med", "q1", "q3", "lanczos"
]


class RioRaster:
    dataset: rio.DatasetReader = None
    source_file_path: Optional[str] = None  # Tracks the physical file origin

    def __init__(self, src: Optional[Union[str, rio.DatasetReader, Path]], prj_path: str = None,
                 source_file_path: str = None):
        self.source_file_path = source_file_path
        if src is not None:
            if isinstance(src, Path): src = str(src)
            # If src is a string path, use it as the source file path
            if isinstance(src, str) and not src.startswith("/vsimem/") and not src.startswith("/vsipythonfilelike/"):
                self.source_file_path = src

            self.set_dataset(src)
            if prj_path is not None and os.path.exists(prj_path):
                self.add_crs_from_prj(prj_path)

    def __repr__(self):
        if self.empty:
            return "<RioRaster: Empty>"
        return f"<RioRaster: {self.dataset.name}, {self.get_image_resolution()}, {self.get_crs()}>"

    @staticmethod
    def ensure_proj():
        proj_dir = pyproj.datadir.get_data_dir()

        os.environ["PROJ_LIB"] = proj_dir
        os.environ["PROJ_DATA"] = proj_dir

        return proj_dir

    def set_dataset(self, src: Union[str, rio.DatasetReader]):
        """
        Set the raster dataset from a source.

        :param src: The source path or DatasetReader object.
        """
        try:
            if isinstance(src, rio.DatasetReader):
                if '/vsipythonfilelike/' in src.name:
                    self.dataset = self.rio_dataset_from_array(src.read(), src.meta)
                else:
                    self.dataset = src
            elif isinstance(src, str):
                if "/vsimem/" in src:
                    with rio.MemoryFile(src) as memfile:
                        self.dataset = memfile.open()
                else:
                    if os.path.exists(src):
                        self.dataset = rio.open(src, mode='r', ignore_cog_layout_break='YES')
                    else:
                        raise FileNotFoundError(f"Raster file not available at {src}")

            if self.dataset is None:
                raise ValueError("Dataset could not be set. It is None.")
        except Exception as e:
            da_logger.exception(f"Error setting dataset: {e}")

    def get_dataset(self) -> rio.DatasetReader:
        """Get the current dataset."""
        return self.dataset

    @staticmethod
    def rio_dataset_from_array(data: np.ndarray, meta, descriptions: list = None) -> rio.DatasetReader:
        """
        Create a RioDataset from an array.

        :param data: The data array.
        :param meta: The metadata.
        :param descriptions: The band descriptions.
        :return: The resulting DatasetReader object.
        """
        bands = 1 if len(data.shape) == 2 else data.shape[0]
        memfile = rio.MemoryFile()
        dst = memfile.open(**meta,
                           compress='lzw',
                           BIGTIFF='YES')
        for i in range(bands):
            d = data if len(data.shape) == 2 else data[i, :, :]
            dst.write(d, i + 1)
        if descriptions is not None:
            for i, desc in enumerate(descriptions):
                if desc:
                    dst.set_band_description(i + 1, desc)
        dst.close()
        return memfile.open()

    def add_crs_from_prj(self, prj_file: str):
        name, ext = os.path.splitext(prj_file)
        if ext.lower() != ".prj":
            return
        with open(prj_file) as f:
            wkt = f.read()
        crs = CRS.from_wkt(wkt)
        meta = self.dataset.meta.copy()
        meta.update(crs=crs)
        # rewrite into MemoryFile preserving data
        data = self.dataset.read()
        self.dataset = self.rio_dataset_from_array(data, meta, list(self.dataset.descriptions or ()))

    def get_meta(self) -> dict:
        """Get the metadata of the current dataset."""
        return self.dataset.meta

    def get_profile(self):
        """Get the profile of the dataset."""
        return self.dataset.profile

    def get_spectral_resolution(self) -> int:
        """
        Get the number of bands (spectral resolution) in the raster.

        :return: Number of bands.
        """
        if self.dataset is not None:
            return self.dataset.count
        else:
            raise ValueError("Dataset is not set.")

    def get_spatial_resolution(self, in_meter=True) -> tuple:
        """
        Return the spatial resolution (pixel size) as (x_resolution, y_resolution).

        If `in_meter` is True and the CRS is geographic (degrees),
        it will approximate the resolution in meters using a degree-to-meter conversion
        at the dataset's center latitude.
        """
        if self.dataset is None:
            raise ValueError("Dataset is not set.")

        a, _, _, _, e, _ = self.dataset.transform[:6]
        x_res, y_res = abs(a), abs(e)

        if in_meter:
            crs = CRS.from_user_input(self.dataset.crs)
            if crs.is_geographic:
                # Get dataset center latitude for conversion
                bounds = self.dataset.bounds
                center_lat = (bounds.top + bounds.bottom) / 2

                # Convert degrees to meters at center latitude
                meters_per_degree_lon = 111320 * math.cos(math.radians(center_lat))
                meters_per_degree_lat = 110540  # average

                x_res *= meters_per_degree_lon
                y_res *= meters_per_degree_lat

        return x_res, y_res

    def get_radiometric_resolution(self) -> str:
        """
        Return the radiometric resolution (bit depth) based on the data type.

        :return: String representing the data type (e.g., 'uint8', 'int16')
        """
        if self.dataset is not None:
            return self.dataset.dtypes[0]  # Assume all bands have the same dtype
        else:
            raise ValueError("Dataset is not set.")

    def get_image_resolution(self) -> tuple:
        """
        Return the image resolution in pixels as (width, height).

        :return: Tuple (width, height)
        """
        if self.dataset is not None:
            return self.dataset.width, self.dataset.height
        else:
            raise ValueError("Dataset is not set.")

    @property
    def empty(self):
        return self.dataset is None

    @staticmethod
    def write_to_file(
            img_des: Union[str, Path], data: np.ndarray,
            crs: CRS, affine_transform: Affine,
            nodata_value: Optional[Union[int, float]] = None,
            band_names: Optional[List[str]] = None,
    ) -> None:
        """Write raster data to a GeoTIFF or COG file.

        This method supports both:
            - 2D arrays with shape (rows, cols), written as a single-band raster
            - 3D arrays with shape (bands, rows, cols), written as a multiband raster

        Args:
            img_des (str):
                Destination raster file path. Use ".cog" extension to write as COG;
                otherwise GeoTIFF is used.

            data (np.ndarray):
                Raster data array. Accepted shapes are:
                    - (rows, cols) for a single-band raster
                    - (bands, rows, cols) for a multiband raster

            crs (CRS):
                Coordinate reference system of the output raster.

            affine_transform (Affine):
                Affine geotransform defining pixel size, rotation, and origin.

            nodata_value:
                NoData value to assign to the output raster. Can be numeric or np.nan
                for floating-point rasters.

            band_names (List[str], optional):
                Optional list of band descriptions. If provided, each name is written
                to the corresponding raster band.

        Raises:
            ValueError:
                If data is not a 2D or 3D NumPy array.
        """
        data = np.asarray(data)

        # ---------------------------------------------------------
        # Handle 2D vs 3D arrays
        # ---------------------------------------------------------
        if data.ndim == 2:
            bands = 1
            rows, cols = data.shape

        elif data.ndim == 3:
            bands, rows, cols = data.shape

        else:
            raise ValueError(
                f"Raster data must be 2D or 3D. Got shape: {data.shape}"
            )

        with rasterio.open(
                img_des,
                "w",
                driver="GTiff",
                height=rows,
                width=cols,
                count=bands,
                dtype=data.dtype,
                crs=crs,
                transform=affine_transform,
                nodata=nodata_value,
                compress="lzw",
        ) as dst:

            # -----------------------------------------------------
            # Single band
            # -----------------------------------------------------
            if bands == 1:

                if data.ndim == 3:
                    dst.write(data[0], 1)
                else:
                    dst.write(data, 1)

            # -----------------------------------------------------
            # Multiband
            # -----------------------------------------------------
            else:

                for i in range(bands):
                    dst.write(data[i], i + 1)

            # -----------------------------------------------------
            # Band names
            # -----------------------------------------------------
            if band_names:

                for i, band_name in enumerate(band_names):
                    dst.set_band_description(i + 1, band_name)

    def save_to_file(self, img_des: str | Path, data: np.ndarray = None, crs: CRS = None,
                     affine_transform: Affine = None, nodata_value=None, band_names: List[str] = ()):
        """
        Save the dataset to a file.

        :param img_des: The destination file path.
        :param data: The data array to save.
        :param crs: The CRS to use.
        :param affine_transform: The affine transform to use.
        :param nodata_value: The no-data value to use.
        :param band_names: The list of band name to write in the file as description
        """
        data = self.get_data_array() if data is None else data
        crs = crs if crs else self.dataset.crs
        affine_transform = affine_transform if affine_transform else self.dataset.transform
        # nodata_value = nodata_value if nodata_value else self.get_nodata_value()
        nodata_value = nodata_value if nodata_value is not None else self.get_nodata_value()
        band_names = band_names if len(band_names) > 0 else self.get_all_band_names()
        self.write_to_file(img_des, data, crs, affine_transform, nodata_value, band_names=band_names)

    def get_data_array_by_band_name(self, target_band_names: List, convert_no_data_2_nan=False) -> np.ndarray:
        band_names = self.dataset.descriptions
        data = []
        for target_band_name in target_band_names:
            if target_band_name in band_names:
                band_index = band_names.index(target_band_name) + 1
                band_data = self.get_data_array(band_index, convert_no_data_2_nan=convert_no_data_2_nan)
                data.append(band_data)
        return np.array(data if len(data) > 1 else data[0])

    def get_data_array(self, band: int = None, convert_no_data_2_nan: bool = False, envelop_gdf=None) -> np.ndarray:
        """
        Get the data array from the dataset, optionally within an envelope.

        :param band: The band number to read (1-based index). Reads all bands if None.
        :param convert_no_data_2_nan: Whether to convert no-data values to NaN.
        :param envelop_gdf: Optional GeoDataFrame containing the envelope geometry to crop data.
        :return: NumPy array of raster values.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        dataset = self.dataset

        if envelop_gdf is not None:
            # Ensure CRS match
            if envelop_gdf.crs != dataset.crs:
                envelop_gdf = envelop_gdf.to_crs(dataset.crs)

            # Mask raster with given envelope geometry
            geom = [envelop_gdf.unary_union]
            data_arr, _ = mask(dataset, geom, crop=True, indexes=band)
        else:
            data_arr = dataset.read(band) if band else dataset.read()

        if convert_no_data_2_nan:
            nodata_val = dataset.nodata
            if nodata_val is not None:
                if not np.issubdtype(data_arr.dtype, np.floating):
                    data_arr = data_arr.astype(np.float32)
                data_arr[data_arr == nodata_val] = np.nan

        return data_arr

    def calculate_pixel_wise_stats(
            self,
            method: Literal["min", "max", "mean", "sum", "std"] = "mean"
    ) -> 'RioRaster':
        """
        stack raster before this operation
        Performs pixel-wise statistics across all bands of the current RioRaster.
        Returns a new single-band RioRaster.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        # 1. Get data array (Bands, Height, Width)
        # We use convert_no_data_2_nan=True to ensure NoData doesn't bias the stats
        data = self.get_data_array(convert_no_data_2_nan=True)

        if data.ndim < 3:
            da_logger.warning("Raster has only one band. Stats will return original values.")
            return self if not hasattr(self, 'copy') else RioRaster(self.dataset)

        # 2. Perform calculation along the Band axis (axis 0)
        if method == "min":
            stat_arr = np.nanmin(data, axis=0)
        elif method == "max":
            stat_arr = np.nanmax(data, axis=0)
        elif method == "mean" or method == "avg":
            stat_arr = np.nanmean(data, axis=0)
        elif method == "sum":
            stat_arr = np.nansum(data, axis=0)
        elif method == "std":
            stat_arr = np.nanstd(data, axis=0)
        else:
            raise ValueError(f"Unsupported method: {method}")

        # 3. Handle pixels where ALL bands were NaN (NumPy often turns these into
        # Infinity or 0 depending on the function). We ensure they stay NaN.
        all_nan_mask = np.all(np.isnan(data), axis=0)
        stat_arr[all_nan_mask] = np.nan

        # 4. Create new single-band metadata
        new_meta = self.get_meta().copy()
        new_meta.update({
            "count": 1,
            "dtype": "float32",
            "nodata": np.nan
        })

        # 5. Return as a new RioRaster
        # Wrap in 3D array for rio_dataset_from_array (1, H, W)
        stat_arr_3d = stat_arr[np.newaxis, ...]

        new_ds = RioRaster.rio_dataset_from_array(
            stat_arr_3d,
            new_meta,
            descriptions=[f"Pixelwise {method}"]
        )

        return RioRaster(new_ds)

    def get_data_shape(self):
        """
        Get the shape of the data array.

        :return: Tuple of (band, row, column).
        """
        data = self.get_data_array()
        bands, rows, cols = 0, 0, 0
        if len(data.shape) == 2:
            bands = 1
            rows, cols = data.shape
        elif len(data.shape) == 3:
            bands, rows, cols = data.shape
        return bands, rows, cols

    def get_crs(self) -> CRS:
        """Get the CRS of the dataset."""
        return self.dataset.crs

    def get_extent_after_skip_rows_cols(self, n_rows_skip, n_cols_skip):
        """
        Get the extent of the dataset after skipping rows and columns.

        :param n_rows_skip: Number of rows to skip.
        :param n_cols_skip: Number of columns to skip.
        :return: The new extent.
        """
        # y_size, x_size = self.get_image_resolution()
        x_size, y_size = self.get_image_resolution()
        geo_t = self.get_geo_transform()
        min_x = geo_t[2] + n_cols_skip * geo_t[0]
        max_y = geo_t[5] + n_rows_skip * geo_t[4]
        max_x = geo_t[2] + geo_t[0] * (x_size - n_cols_skip)
        min_y = geo_t[5] + geo_t[4] * (y_size - n_rows_skip)
        return min_x, min_y, max_x, max_y

    def get_envelop(self, n_rows_skip: int = 0, n_cols_skip: int = 0, srid: int = 0) -> gpd.GeoDataFrame:
        """
        Build the raster envelope as a single-row GeoDataFrame.

        :param n_rows_skip: Number of rows to skip (top/bottom handling should be in your extent fn).
        :param n_cols_skip: Number of columns to skip (left/right handling should be in your extent fn).
        :param srid: Optional EPSG code to reproject the envelope to.
        :return: GeoDataFrame with one polygon row representing the envelope.
        """
        if self.dataset is None:
            raise ValueError("Dataset is not set.")

        # Get extent with or without skipping
        if n_rows_skip or n_cols_skip:
            minx, miny, maxx, maxy = self.get_extent_after_skip_rows_cols(n_rows_skip, n_cols_skip)
        else:
            minx, miny, maxx, maxy = self.get_raster_extent()

        # Build polygon in the dataset's native CRS
        gdf = gpd.GeoDataFrame(
            {"id": [1]},
            geometry=[box(minx, miny, maxx, maxy)],
            crs=self.get_crs()  # should return a pyproj CRS or EPSG int
        )

        # Reproject if requested (to_crs returns a new GeoDataFrame)
        if srid:
            gdf = gdf.to_crs(epsg=srid)

        return gdf

    def get_raster_extent(self) -> list:
        """Get the extent of the raster."""
        bounds = self.dataset.bounds
        return [bounds.left, bounds.bottom, bounds.right, bounds.top]

    def get_raster_srid(self) -> int:
        """
        Get the spatial reference ID (SRID) of the raster.

        :return: The SRID or 0 if unavailable.
        """
        self.ensure_proj()
        if self.dataset is None:
            raise ValueError("Dataset is not set.")

        try:
            crs = self.dataset.crs
            if crs is None:
                return 0

            # Try EPSG directly
            epsg_code = crs.to_epsg()
            if epsg_code:
                return epsg_code

            # If not EPSG, try parsing from WKT
            crs_obj = CRS.from_wkt(str(crs))
            return crs_obj.to_epsg() or 0

        except Exception as e:
            da_logger.exception(f"Error getting SRID: {e}")
            return 0

    def get_geo_transform(self) -> Affine:
        """
        Get the affine transform of the dataset.

        :return: The affine transform.
            the sequence is [a,b,c,d,e,f]
        """
        return self.dataset.transform

    def get_nodata_value(self):
        """Get the no-data value of the dataset."""
        return self.dataset.nodata

    def set_nodata(self, nodata_value=None):
        """Set NoData value for the raster (requires writable dataset, e.g. mode='r+')."""
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        # Must be writable
        if self.dataset.mode not in ("r+", "w", "w+"):
            raise RuntimeError(
                f"Dataset is read-only (mode='{self.dataset.mode}'). "
                "Reopen with mode='r+' (or write to a new file) to set NoData."
            )

        # Choose a sensible default if not provided
        dtype = np.dtype(self.dataset.dtypes[0])
        if nodata_value is None:
            if dtype.kind in ("f",):  # float
                nodata_value = np.finfo(dtype).max  # or use np.nan if your workflow/driver supports it
            else:  # int/uint
                nodata_value = np.iinfo(dtype).min

        # Set NoData (no update_tags on DatasetReader; writable dataset will persist on close)
        self.dataset.nodata = nodata_value
        return nodata_value

    @staticmethod
    def _sanitize_nodata(img_arr, nodata_value):
        """
        Ensure nodata value is compatible with raster dtype.
        """

        dtype = np.dtype(img_arr.dtype)

        if nodata_value is None:
            return None

        # ----------------------------------------
        # Handle NaN nodata safely
        # ----------------------------------------
        is_nan = False

        try:
            is_nan = np.isnan(nodata_value)
        except (TypeError, ValueError):
            is_nan = False

        if is_nan:
            if np.issubdtype(dtype, np.floating):
                return np.nan

            da_logger.warning(
                f"Replacing NaN nodata with 0 for integer raster ({dtype})"
            )
            return 0

        # ----------------------------------------
        # Integer dtype validation
        # ----------------------------------------
        if np.issubdtype(dtype, np.integer):

            info = np.iinfo(dtype)

            try:
                if nodata_value < info.min or nodata_value > info.max:
                    safe_nodata = 0

                    da_logger.warning(
                        f"Nodata value {nodata_value} outside "
                        f"{dtype} range [{info.min}, {info.max}]. "
                        f"Using {safe_nodata} instead."
                    )

                    return safe_nodata

            except TypeError:

                da_logger.warning(
                    f"Invalid nodata value '{nodata_value}' "
                    f"for integer raster ({dtype}). Using 0."
                )

                return 0

        # ----------------------------------------
        # Floating dtype validation
        # ----------------------------------------
        if np.issubdtype(dtype, np.floating):

            try:
                return float(nodata_value)
            except (TypeError, ValueError):

                da_logger.warning(
                    f"Invalid floating nodata value '{nodata_value}'. "
                    f"Using NaN."
                )

                return np.nan

        return nodata_value

    def rio_raster_from_array(self, img_arr: np.ndarray) -> 'RioRaster':
        """
        Create a RioRaster object from an array.

        :param img_arr: The image array.
        :return: A new RioRaster object.
        """
        # meta_data = self.get_meta().copy()
        raster = self.raster_from_array(img_arr, crs=self.get_crs(),
                                        g_transform=self.get_geo_transform(),
                                        nodata_value=self.get_nodata_value())
        return raster

    @staticmethod
    def raster_from_array(img_arr: np.ndarray, crs: Union[str, CRS],
                          g_transform: Affine, nodata_value=None) -> Union['RioRaster', None]:
        """
        Create a RioRaster object from an array.

        :param img_arr: The image array.
        :param crs: The CRS to use.
        :param g_transform: The affine transform to use.
        :param nodata_value: The no-data value to use.
        :return: A new RioRaster object.
        """
        try:
            nodata_value = RioRaster._sanitize_nodata(
                img_arr,
                nodata_value,
            )
            memfile = rio.MemoryFile()
            if len(img_arr.shape) == 2:
                bands = 1
                rows, cols = img_arr.shape
            else:
                bands, rows, cols = img_arr.shape

            with memfile.open(driver='GTiff',
                              height=rows,
                              width=cols,
                              count=bands,
                              dtype=str(img_arr.dtype),
                              crs=crs,
                              transform=g_transform,
                              nodata=nodata_value,
                              compress='lzw',
                              BIGTIFF='YES') as dataset:
                for i in range(bands):
                    d = img_arr if len(img_arr.shape) == 2 else img_arr[i, :, :]
                    dataset.write(d, i + 1)
                dataset.close()

            dataset = memfile.open()  # Reopen as DatasetReader
            new_raster = RioRaster(dataset)
            return new_raster

        except Exception as e:
            da_logger.exception(f"Error creating raster from array: {e}")
            return None

    def get_bounds(self) -> tuple:
        """
        Get the bounding box of the raster in the format (minx, miny, maxx, maxy).

        :return: Tuple of (minx, miny, maxx, maxy).
        """
        if self.dataset is not None:
            # return self.dataset.bounds  # returns BoundingBox(minx, miny, maxx, maxy)
            return tuple(self.dataset.bounds)
        else:
            raise ValueError("Dataset is not set.")

    def clip_raster(
            self,
            aoi: Union[gpd.GeoDataFrame, shapely.geometry.Polygon, shapely.geometry.MultiPolygon],
            in_place: bool = True,
            crs: Union[int, str, CRS] = None,
            clip_within_aoi: bool = True,  # Default to True to see the effect
            nodata_value: Optional[Union[int, float]] = None
    ) -> Optional['RioRaster']:

        if self.dataset is None:
            raise RuntimeError("Dataset is not set.")

        # 1. Standardize AOI
        if isinstance(aoi, (shapely.geometry.Polygon, shapely.geometry.MultiPolygon)):
            aoi = gpd.GeoDataFrame(geometry=[aoi], crs=crs)
        if aoi.crs is None:
            aoi.set_crs(crs or self.get_crs(), inplace=True)

        # 2. Align CRS
        if not aoi.crs.equals(self.get_crs()):
            aoi = aoi.to_crs(self.get_crs())

        # 3. Intersection Check
        raster_box = box(*self.get_bounds())
        intersecting_aoi = aoi[aoi.intersects(raster_box)]

        if intersecting_aoi.empty:
            da_logger.warning("❌ No intersection found.")
            return None

        # 4. Define Geometries
        if clip_within_aoi:
            geometries = [geom for geom in intersecting_aoi.geometry if geom.is_valid]
        else:
            geometries = [box(*intersecting_aoi.total_bounds)]

        # 5. Determine NoData
        if nodata_value is None:
            nodata_value = self.get_nodata_value()
            if nodata_value is None:
                dtype = np.dtype(self.dataset.dtypes[0])
                nodata_value = 0 if np.issubdtype(dtype, np.integer) else np.nan

        # 6. MASK OPERATION
        # We use all_touched=True to ensure we don't lose thin pixels
        out_img, out_transform = mask(
            self.dataset,
            geometries,
            crop=True,
            nodata=nodata_value,
            filled=True,
            all_touched=True
        )

        # --- THE "FORCE" FIX ---
        # Sometimes rasterio's internal mask doesn't fill perfectly if the geometries
        # are complex. We can force a secondary mask if necessary, but 'filled=True'
        # with a valid 'nodata' usually does the trick.
        # -----------------------

        # 7. Metadata update
        out_meta = self.dataset.meta.copy()
        out_meta.update({
            "height": out_img.shape[1],
            "width": out_img.shape[2],
            "transform": out_transform,
            "nodata": nodata_value,  # CRITICAL: This must be in metadata
            "count": self.dataset.count
        })

        # 8. Create new Dataset
        new_ds = self.rio_dataset_from_array(out_img, out_meta, list(self.dataset.descriptions or []))

        if in_place:
            self.dataset.close()
            self.dataset = new_ds
            return self
        return RioRaster(new_ds)

    def reproject_to(self, target_crs: Union[str, CRS], in_place=False,
                     resampling: ResampleMethod = "nearest") -> 'RioRaster':
        """
        Reproject the current raster to the specified CRS.

        :param target_crs: CRS to reproject to (e.g., 'EPSG:4326' or rasterio.CRS object)
        :param in_place: If True, modifies self. Otherwise returns a new RioRaster.
        :return: Reprojected RioRaster object or self (if in_place=True)
        """
        self.ensure_proj()
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        if not hasattr(Resampling, resampling):
            raise ValueError(f"Unsupported resampling '{resampling}'.")
        resampling_enum = getattr(Resampling, resampling)

        target_crs = CRS.from_user_input(target_crs)
        transform, width, height = calculate_default_transform(
            self.dataset.crs, target_crs,
            self.dataset.width, self.dataset.height,
            *self.dataset.bounds
        )
        kwargs = self.dataset.meta.copy()
        kwargs.update({'crs': target_crs, 'transform': transform, 'width': width, 'height': height})
        if self.dataset.nodata is not None:
            kwargs['nodata'] = self.dataset.nodata

        memfile = rio.MemoryFile()
        with memfile.open(**kwargs) as dst:
            for i in range(1, self.dataset.count + 1):
                reproject(
                    source=rio.band(self.dataset, i),
                    destination=rio.band(dst, i),
                    src_transform=self.dataset.transform,
                    src_crs=self.dataset.crs,
                    dst_transform=transform,
                    dst_crs=target_crs,
                    resampling=resampling_enum
                )
            # descriptions…

            # Set band descriptions if available
            descriptions = self.dataset.descriptions
            if descriptions:
                for i, desc in enumerate(descriptions):
                    if desc:
                        dst.set_band_description(i + 1, desc)

        new_raster = RioRaster(memfile.open())
        if in_place:
            self.dataset = new_raster.dataset
            return self
        else:
            return new_raster

    # def pad_raster(self, des_raster: 'RioRaster', in_place: bool = True) -> Union[None, 'RioRaster']:
    #     src_crs = self.get_crs()
    #     des_crs = des_raster.get_crs()
    #
    #     # Check CRS compatibility
    #     if src_crs != des_crs:
    #         da_logger.debug("🔄 CRS mismatch. Reprojecting source raster to match destination raster CRS...")
    #         self.reproject_to(des_crs, in_place=True)
    #
    #     aff: Affine = self.get_geo_transform()
    #     des_bounds = des_raster.get_bounds()
    #
    #     rows, cols = rio.transform.rowcol(
    #         aff,
    #         xs=[des_bounds[0], des_bounds[2]],
    #         ys=[des_bounds[3], des_bounds[1]],
    #     )
    #
    #     height = rows[1] - rows[0]
    #     width = cols[1] - cols[0]
    #
    #     window = windows.Window(col_off=cols[0], row_off=rows[0], width=width, height=height)
    #     window_transform = windows.transform(window, aff)
    #
    #     kwargs = self.dataset.meta.copy()
    #     kwargs.update({
    #         'crs': self.get_crs(),
    #         'transform': window_transform,
    #         'width': width,
    #         'height': height
    #     })
    #
    #     memfile = rio.MemoryFile()
    #     dst = memfile.open(**kwargs)
    #
    #     fill = self.dataset.nodata
    #     if fill is None:
    #         fill = np.nan if np.issubdtype(np.dtype(self.dataset.dtypes[0]), np.floating) else 0
    #
    #     data = self.dataset.read(window=window, boundless=True, fill_value=fill)
    #     dst.write(data)
    #     dst.close()
    #     result = RioRaster(memfile.open())
    #
    #     if in_place:
    #         self.dataset = result.dataset
    #         return None
    #     else:
    #         return result

    def pad_raster(self, des_raster: 'RioRaster', in_place: bool = True) -> Union[None, 'RioRaster']:
        src_crs = self.get_crs()
        des_crs = des_raster.get_crs()

        # 1) CRS compatibility
        if src_crs != des_crs:
            da_logger.debug("🔄 CRS mismatch. Reprojecting source raster to match destination raster CRS...")
            self.reproject_to(des_crs, in_place=True)

        aff: Affine = self.get_geo_transform()
        des_bounds = des_raster.get_bounds()

        # 2) Compute window in source pixel coords
        rows, cols = rio.transform.rowcol(
            aff,
            xs=[des_bounds[0], des_bounds[2]],
            ys=[des_bounds[3], des_bounds[1]],
        )

        height = rows[1] - rows[0]
        width = cols[1] - cols[0]

        window = windows.Window(col_off=cols[0], row_off=rows[0], width=width, height=height)
        window_transform = windows.transform(window, aff)

        # 3) Build new meta, but FORCE float32 + NaN nodata
        src_meta = self.dataset.meta.copy()
        src_nodata = self.dataset.nodata
        src_dtype = np.dtype(self.dataset.dtypes[0])

        kwargs = src_meta.copy()
        kwargs.update({
            "crs": self.get_crs(),
            "transform": window_transform,
            "width": width,
            "height": height,
            "dtype": "float32",  # <- force float
            "nodata": np.nan,  # <- nodata is NaN
        })

        memfile = rio.MemoryFile()
        dst = memfile.open(**kwargs)

        # 4) Read from source, then convert to float32 / NaN
        #    Use src_nodata as fill, then map it to NaN after cast.
        fill = src_nodata
        if fill is None:
            # If original is float, NaN is ok; if int, use some fill (will be turned to NaN anyway)
            if np.issubdtype(src_dtype, np.floating):
                fill = np.nan
            else:
                # choose a fill that is likely nodata; will be converted to NaN anyway
                fill = 0

        data = self.dataset.read(window=window, boundless=True, fill_value=fill)
        data = data.astype("float32", copy=False)

        # Map original nodata to NaN (for float stack)
        if src_nodata is not None and not (isinstance(src_nodata, float) and np.isnan(src_nodata)):
            mask = (data == src_nodata)
            data[mask] = np.nan
        # If we chose fill manually above and we know it's a fake nodata, you can also map that to NaN:
        # else: if we used some special fill, you could map that here.

        dst.write(data)
        dst.close()
        result = RioRaster(memfile.open())

        if in_place:
            self.dataset = result.dataset
            return None
        else:
            return result

    def reclassify_raster(
            self,
            thresholds: Union[dict, List[tuple]],
            band: int = None,
            nodata_val: int = 0
    ) -> 'RioRaster':
        """
        Reclassify a single-band raster using defined threshold rules.

        :param thresholds: Reclassification rules in dict or list format.
            Dict example:
                {
                    "water": (('lt', 0.015), 4),
                    "built-up": ((0.015, 0.02), 1),
                    "barren": ((0.07, 0.27), 2),
                    "vegetation": (('gt', 0.27), 3)
                }
            List example:
                [
                    (('lt', 0.015), 4),
                    ((0.015, 0.02), 1),
                    ((0.07, 0.27), 2),
                    (('gt', 0.27), 3)
                ]

        :param band: 1-based band number. If not specified and multiple bands exist, raises error.
        :param nodata_val: Optional fallback NoData value.
        :return: A new reclassified single-band RioRaster.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        band_count = self.get_spectral_resolution()
        if band_count > 1 and band is None:
            raise ValueError("Multiple bands detected. Specify 'band' explicitly for reclassification.")

        band = band or 1
        img_arr = self.get_data_array(band)
        img_arr = np.squeeze(img_arr)
        # nodata_val = nodata
        # Support both dict and list threshold formats
        if isinstance(thresholds, dict):
            threshold_rules = thresholds
        elif isinstance(thresholds, list):
            threshold_rules = {
                i: rule for i, rule in enumerate(thresholds)
            }
        else:
            raise TypeError("Thresholds must be a dict or a list of tuples.")

        classified = BandProcess.reclassify_band(
            img_arr,
            threshold_rules,
            nodata_val
        )

        result_array = np.expand_dims(classified.astype(np.uint8), axis=0)

        return RioRaster.raster_from_array(
            result_array,
            crs=self.get_crs(),
            g_transform=self.get_geo_transform(),
            nodata_value=nodata_val
        )

    def get_masked_array(self, band=1) -> np.ma.MaskedArray:
        data = self.get_data_array(band)
        nodata = self.get_nodata_value()
        if nodata is None:
            return np.ma.masked_array(data, mask=False)
        if np.issubdtype(data.dtype, np.floating) and np.isnan(nodata):
            mask = np.isnan(data)
        else:
            mask = (data == nodata)
        return np.ma.masked_array(data, mask=mask)

    # def to_xarray(self):
    #     """
    #     Convert the raster to xarray.DataArray for advanced analysis.
    #
    #     :return: xarray.DataArray object.
    #     """
    #     import rioxarray  # extends xarray with rasterio support
    #
    #     if self.dataset is None:
    #         raise ValueError("Raster dataset is empty.")
    #
    #     # rioxarray.open_rasterio returns an xarray.DataArray
    #     return rioxarray.open_rasterio(self.dataset.name, masked=True)

    @staticmethod
    def create_reference_raster(
            output_path: str | Path,
            width: Optional[int] = None,
            height: Optional[int] = None,
            spatial_resolution: Optional[float] = None,
            count: int = 1,
            crs: str = "EPSG:4326",
            extent: Optional[Tuple[float, float, float, float]] = None,
            dtype: Union[np.dtype, str] = "float32",
            fill_value: Union[int, float] = 1,
            nodata: Optional[Union[int, float]] = np.nan,
            aoi: Optional[gpd.GeoDataFrame] = None,
            all_touched: bool = True,
    ) -> str:
        """
            Create a reference GeoTIFF aligned to a specified extent and resolution,
            with optional masking by an Area of Interest (AOI).

            The output raster is initialized with `fill_value` inside the valid domain
            and `nodata` outside the AOI (if provided).

            Supported input combinations
            ----------------------------
            1. AOI + spatial_resolution
               - The extent is derived from the AOI bounds.
               - Width and height are computed automatically.

            2. extent + spatial_resolution
               - The raster extent is provided explicitly.
               - Width and height are computed automatically.

            3. extent + width + height
               - The raster dimensions are specified directly.

            Parameters
            ----------
            output_path : str | Path
                Output GeoTIFF path.

            aoi : GeoDataFrame, optional
                Area of interest used for masking and, if extent is not provided,
                for deriving the raster extent.

            extent : tuple(minx, miny, maxx, maxy), optional
                Spatial extent of the raster.

            spatial_resolution : float, optional
                Pixel size in CRS units. Used to derive width and height.

            width : int, optional
                Raster width in pixels. Must be used together with height.

            height : int, optional
                Raster height in pixels. Must be used together with width.

            fill_value : int | float, default=1
                Value assigned to pixels inside the valid area.

            nodata : int | float, default=np.nan
                Value assigned to pixels outside the AOI.

            Returns
            -------
            str
                Path to the created GeoTIFF.

            Examples
            --------
            # AOI + resolution
            create_reference_raster(
                output_path="ref.tif",
                aoi=aoi_gdf,
                spatial_resolution=500
            )

            # Extent + resolution
            create_reference_raster(
                output_path="ref.tif",
                extent=(minx, miny, maxx, maxy),
                spatial_resolution=500
            )

            # Extent + width + height
            create_reference_raster(
                output_path="ref.tif",
                extent=(minx, miny, maxx, maxy),
                width=1000,
                height=800
            )
        """

        output_path = Path(output_path)

        if output_path.exists():
            return str(output_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        if aoi is not None:
            if aoi.crs is None:
                aoi = aoi.set_crs(crs)
            if not aoi.crs.equals(crs):
                aoi = aoi.to_crs(crs)

            if extent is None:
                extent = tuple(aoi.total_bounds)

        if extent is None:
            raise ValueError("Either extent or aoi must be provided.")

        minx, miny, maxx, maxy = extent

        if spatial_resolution is not None:
            width = int(np.ceil((maxx - minx) / spatial_resolution))
            height = int(np.ceil((maxy - miny) / spatial_resolution))
        elif width is None or height is None:
            raise ValueError("Either spatial_resolution or both width and height must be provided.")

        transform = from_bounds(minx, miny, maxx, maxy, width, height)

        dtype = np.dtype(dtype)

        data = np.full(
            (count, height, width),
            fill_value,
            dtype=dtype,
        )

        if aoi is not None:
            from rasterio.features import rasterize

            shapes = [
                (geom, 1)
                for geom in aoi.geometry
                if geom is not None and not geom.is_empty and geom.is_valid
            ]

            mask_arr = rasterize(
                shapes=shapes,
                out_shape=(height, width),
                transform=transform,
                fill=0,
                default_value=1,
                dtype="uint8",
                all_touched=all_touched,
            )

            for b in range(count):
                data[b][mask_arr == 0] = nodata

        with rasterio.open(
                output_path,
                "w",
                driver="GTiff",
                height=height,
                width=width,
                count=count,
                dtype=dtype,
                crs=crs,
                transform=transform,
                nodata=nodata,
                compress="lzw",
        ) as dst:
            dst.write(data)

        return str(output_path)

    def get_band_colormap(self, band_no: int = 1) -> dict:
        """
        Reads the colormap from the specified band of the raster dataset.

        Parameters:
            band_no (int): The band number to read the colormap from. Defaults to 1.

        Returns:
            dict: A dictionary mapping pixel values to RGB(A) tuples.
                  - Keys are integer pixel values (e.g., 0, 1, 2, ...)
                  - Values are tuples of (R, G, B) or (R, G, B, A) where each is in the range 0–255
        """
        src = self.get_dataset()  # no context manager here
        try:
            colormap = src.colormap(band_no)
            return colormap if colormap else {}
        except ValueError as e:
            if "NULL color table" in str(e):
                return {}
            else:
                raise

    def get_lookup_table(
            self,
            band_no: int = 1,
            class_column: str = "class_name",
            value_column: str = "pixel_value",
            color_column: str = "color"
    ) -> pd.DataFrame:
        """
        Extracts or generates a lookup table DataFrame for a categorical raster.
        Safely cross-references GDAL Category Names with the actual underlying
        band colormap to prevent fallback color bleaching (#ffffff).

        Parameters
        ----------
        band_no : int, default=1
            The 1-based band index to analyze.
        class_column : str, default="class_name"
            Name of the column for class descriptions.
        value_column : str, default="pixel_value"
            Name of the column for integer pixel values.
        color_column : str, default="color"
            Name of the column for Hex colors.

        Returns
        -------
        pd.DataFrame
            DataFrame with columns matching value_column, color_column, and class_column.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        gdal.UseExceptions()

        # Pre-fetch the rasterio colormap to ensure we have access to real colors
        cmap = self.get_band_colormap(band_no)
        nodata_val = self.get_nodata_value()

        # 1. Attempt to read via GDAL (Category Names or RAT)
        file_name = self.get_file_name()
        if file_name and os.path.exists(file_name):
            try:
                ds = gdal.Open(file_name, gdal.GA_ReadOnly)
                if ds is not None:
                    band = ds.GetRasterBand(band_no)
                    cat_names = band.GetCategoryNames()
                    rat = band.GetDefaultRAT()

                    # Path A: Explicit Raster Attribute Table exists
                    if rat is not None and rat.GetRowCount() > 0:
                        rows = []
                        val_col_idx = max(0, rat.GetColOfUsage(gdal.GFU_Generic))
                        name_col_idx = max(1, rat.GetColOfUsage(gdal.GFU_Name))
                        r_idx = rat.GetColOfUsage(gdal.GFU_Red)
                        g_idx = rat.GetColOfUsage(gdal.GFU_Green)
                        b_idx = rat.GetColOfUsage(gdal.GFU_Blue)

                        for i in range(rat.GetRowCount()):
                            val = rat.GetValueAsInt(i, val_col_idx)
                            if nodata_val is not None and val == nodata_val:
                                continue

                            label = rat.GetValueAsString(i, name_col_idx)

                            # Fallback directly to our colormap if RAT RGB columns are missing/invalid
                            if r_idx != -1 and g_idx != -1 and b_idx != -1:
                                r = rat.GetValueAsInt(i, r_idx)
                                g = rat.GetValueAsInt(i, g_idx)
                                b = rat.GetValueAsInt(i, b_idx)
                                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                            elif val in cmap:
                                r, g, b = cmap[val][:3]
                                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                            else:
                                hex_color = "#ffffff"

                            rows.append({
                                value_column: val,
                                class_column: label,
                                color_column: hex_color
                            })

                        ds = None
                        if rows:
                            return pd.DataFrame(rows)

                    # Path B: Category names exist without a full RAT
                    elif cat_names:
                        rows = []
                        for val, label in enumerate(cat_names):
                            if nodata_val is not None and val == nodata_val:
                                continue
                            # Skip unassigned structural indices, but keep valid names
                            if not label or label.strip() == "":
                                continue

                            # Pull from colormap first!
                            if val in cmap:
                                r, g, b = cmap[val][:3]
                                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                            else:
                                # Only turn white if the pixel value literally doesn't exist in the colormap
                                hex_color = "#ffffff"

                            rows.append({
                                value_column: int(val),
                                class_column: str(label),
                                color_column: hex_color
                            })

                        ds = None
                        if rows:
                            return pd.DataFrame(rows)
            except Exception as e:
                da_logger.debug(f"GDAL category parser bypassed: {e}")
            finally:
                ds = None

        # 2. Fallback: Parse the built-in Rasterio colormap table mapping exclusively
        if cmap:
            rows = []
            for val, rgba in cmap.items():
                if nodata_val is not None and val == nodata_val:
                    continue

                r, g, b = rgba[:3]
                hex_color = f"#{r:02x}{g:02x}{b:02x}"
                rows.append({
                    value_column: int(val),
                    class_column: f"Class {val}",
                    color_column: hex_color
                })
            return pd.DataFrame(rows)

        # 3. Last Resort: No metadata or colors found at all
        da_logger.warning("No colormap or attributes found. Extracting unique values from array.")
        data_arr = self.get_data_array(band_no)
        if nodata_val is not None:
            data_arr = data_arr[data_arr != nodata_val]

        unique_vals = np.unique(data_arr)
        return pd.DataFrame({
            value_column: [int(v) for v in unique_vals],
            class_column: [f"Class {v}" for v in unique_vals],
            color_column: ["#ffffff"] * len(unique_vals)
        })

    @staticmethod
    def build_lookup_table(
            pixel_values: list,
            colors: list,
            class_names: list,
            *,
            nodata_value: int | None = None,
            nodata_color: str = "#000000",
            nodata_label: str = "NoData",
    ) -> pd.DataFrame:
        """
        Build a lookup table DataFrame for classified rasters.

        Parameters
        ----------
        pixel_values : list[int]
            List of pixel/class values.
        colors : list[str | tuple]
            List of colors (hex strings like '#ffbb22' or RGB/RGBA tuples).
        class_names : list[str]
            List of class labels.
        nodata_value : int | None, optional
            If provided, prepends a NoData row to the LUT.
        nodata_color : str, optional
            Color for NoData (default black).
        nodata_label : str, optional
            Label for NoData.

        Returns
        -------
        pd.DataFrame
            DataFrame with columns: ['pixel_value', 'color', 'class_name']
        """

        if not (len(pixel_values) == len(colors) == len(class_names)):
            raise ValueError(
                "pixel_values, colors, and class_names must have the same length"
            )

        df = pd.DataFrame({
            "pixel_value": [int(v) for v in pixel_values],
            "color": colors,
            "class_name": class_names,
        })

        # Ensure correct dtypes
        df["pixel_value"] = df["pixel_value"].astype(int)
        df["class_name"] = df["class_name"].astype(str)

        # Optional NoData row (inserted at top)
        if nodata_value is not None:
            nodata_row = pd.DataFrame([{
                "pixel_value": int(nodata_value),
                "color": nodata_color,
                "class_name": nodata_label,
            }])
            df = pd.concat([nodata_row, df], ignore_index=True)

        return df

    def save_lookup_table(
            self,
            lookup_df: pd.DataFrame = None,
            *,
            pixel_values=None,
            colors=None,
            class_names=None,
    ):
        if lookup_df is None:
            lookup_df = RioRaster.build_lookup_table(
                pixel_values=pixel_values,
                colors=colors,
                class_names=class_names,
            )

        self.save_with_colormap_and_lookup_table(
            output_fp=self.get_file_name(),
            lookup_df=lookup_df,
            nodata_value=self.get_nodata_value(),
        )

    def save_with_colormap_and_lookup_table(
            self,
            output_fp: Union[str, Path],
            lookup_df: pd.DataFrame,
            band_no: int = 1,
            class_column: str = "class_name",
            value_column: str = "pixel_value",
            color_column: str = "color",
            nodata_value: int = 0,
            is_hex: bool = True,
            band_names: List[str] = (),
    ):
        """
        Create a new raster (single-band) with a colormap and a Raster Attribute Table (RAT),
        writing entries only for pixel values actually present in the raster data.

        Parameters:
            output_fp (str): Output raster path.
            lookup_df (pd.DataFrame): DataFrame with pixel value, color, and class name.
            band_no (int): Band number to process (default: 1).
            class_column (str): Column with class descriptions.
            value_column (str): Column with pixel values.
            color_column (str): Column with color values (hex or RGB string/tuple).
            nodata_value (int): Value used as NoData in the raster.
            is_hex (bool): Whether the color is given in hex format (e.g. "#ffbb22"). If False, assumes RGB strings or tuples.

        Also writes a band description (band name) if provided.
        """

        gdal.UseExceptions()
        if isinstance(output_fp, Path): output_fp = str(output_fp)

        def parse_color(c):
            """Parses a color from hex or RGB string/tuple into an (R, G, B) tuple"""
            if is_hex and isinstance(c, str) and c.startswith("#"):
                c = c.lstrip("#")
                return tuple(int(c[idx:idx + 2], 16) for idx in (0, 2, 4))
            if isinstance(c, str):
                return tuple(map(int, c.strip("() ").split(",")))
            return tuple(int(x) for x in c)

        # ---- Read & prepare raster data and metadata ----
        raster_data = self.get_data_array(band_no).astype("uint8")
        meta = self.get_meta().copy()
        meta.update(dtype="uint8", count=1, nodata=nodata_value)

        # Allowed pixel values from LUT
        allowed_values = set(lookup_df[value_column].astype(int).unique())

        # Replace invalids with NoData
        mask_invalid = ~np.isin(raster_data, list(allowed_values))
        raster_data[mask_invalid] = nodata_value

        present_values = set(np.unique(raster_data)) - {nodata_value}
        lut = lookup_df[lookup_df[value_column].isin(present_values)].copy()

        # Colormap
        colormap = {int(r[value_column]): parse_color(r[color_column]) for _, r in lut.iterrows()}
        if nodata_value not in colormap:
            colormap[nodata_value] = (0, 0, 0)

        # ---- Write raster + colormap using rasterio ----
        band_desc = None
        if band_names and len(band_names) > 0:
            band_desc = str(band_names[0])

        with rasterio.open(output_fp, "w", **meta) as dst:
            dst.write(raster_data, 1)
            dst.write_colormap(1, colormap)

            # Write band description (band name)
            if band_desc:
                dst.set_band_description(1, band_desc)

        # ---- Write RAT with GDAL ----
        ds = gdal.Open(output_fp, gdal.GA_Update)
        if ds is None:
            raise RuntimeError(f"Failed to open {output_fp} with GDAL for RAT writing.")
        band = ds.GetRasterBand(1)

        # Also set description via GDAL (helps some GIS)
        if band_desc:
            try:
                band.SetDescription(band_desc)
            except Exception:
                pass

        # Prepare LUT sorted
        lut_sorted = lut.copy()
        lut_sorted[value_column] = lut_sorted[value_column].astype(int)
        lut_sorted = lut_sorted.sort_values(by=value_column).reset_index(drop=True)

        rat = gdal.RasterAttributeTable()
        rat.CreateColumn("Value", gdal.GFT_Integer, gdal.GFU_Generic)
        rat.CreateColumn("Label", gdal.GFT_String, gdal.GFU_Name)
        rat.CreateColumn("Red", gdal.GFT_Integer, gdal.GFU_Red)
        rat.CreateColumn("Green", gdal.GFT_Integer, gdal.GFU_Green)
        rat.CreateColumn("Blue", gdal.GFT_Integer, gdal.GFU_Blue)
        rat.SetRowCount(len(lut_sorted))

        for i, (_, row) in enumerate(lut_sorted.iterrows()):
            val = int(row[value_column])
            r, g, b = parse_color(row[color_column])
            label = str(row[class_column]) if pd.notnull(row[class_column]) else f"Class {val}"
            rat.SetValueAsInt(i, 0, val)
            rat.SetValueAsString(i, 1, label)
            rat.SetValueAsInt(i, 2, r)
            rat.SetValueAsInt(i, 3, g)
            rat.SetValueAsInt(i, 4, b)

        band.SetDefaultRAT(rat)

        # ---- Category names ----
        max_val = max([nodata_value] + list(present_values)) if present_values else nodata_value
        size = max(max_val + 1, 256)
        cat_names = [""] * size
        if nodata_value < size:
            cat_names[nodata_value] = "NoData"

        for _, row in lut_sorted.iterrows():
            v = int(row[value_column])
            if v < size:
                cat_names[v] = str(row[class_column]) if pd.notnull(row[class_column]) else f"Class {v}"

        try:
            band.SetCategoryNames(cat_names)
        except Exception:
            pass

        ds.FlushCache()
        ds = None  # IMPORTANT: close dataset to persist RAT reliably

    # def save_with_colormap_and_lookup_table(
    #         self,Z
    #         output_fp: str,
    #         lookup_df: pd.DataFrame,
    #         band_no: int = 1,
    #         class_column: str = "class_name",
    #         value_column: str = "pixel_value",
    #         color_column: str = "color",
    #         nodata_value: int = 0,
    #         is_hex=True
    # ):
    #     """
    #     Create a new raster (single-band) with a colormap and a Raster Attribute Table (RAT),
    #     writing entries only for pixel values actually present in the raster data.
    #
    #     Parameters:
    #         output_fp (str): Output raster path.
    #         lookup_df (pd.DataFrame): DataFrame with pixel value, color, and class name.
    #         band_no (int): Band number to process (default: 1).
    #         class_column (str): Column with class descriptions.
    #         value_column (str): Column with pixel values.
    #         color_column (str): Column with color values (hex or RGB string/tuple).
    #         nodata_value (int): Value used as NoData in the raster.
    #         is_hex (bool): Whether the color is given in hex format (e.g. "#ffbb22"). If False, assumes RGB strings or tuples.
    #     """
    #
    #     # Enable explicit error handling in GDAL
    #     gdal.UseExceptions()
    #
    #     def parse_color(c):
    #         """Parses a color from hex or RGB string/tuple into an (R, G, B) tuple"""
    #         if is_hex and isinstance(c, str) and c.startswith("#"):
    #             c = c.lstrip("#")
    #             return tuple(int(c[idx:idx + 2], 16) for idx in (0, 2, 4))
    #         if isinstance(c, str):
    #             return tuple(map(int, c.strip("() ").split(",")))
    #         return tuple(int(x) for x in c)
    #
    #     # ---- Read & prepare raster data and metadata ----
    #     raster_data = self.get_data_array(band_no).astype("uint8")
    #     meta = self.get_meta().copy()
    #     meta.update(dtype="uint8", count=1, nodata=nodata_value)
    #
    #     # Get the set of allowed pixel values from the lookup table
    #     allowed_values = set(lookup_df[value_column].astype(int).unique())
    #
    #     #
    #     # Replace all values not in lookup with nodata_value
    #     mask_invalid = ~np.isin(raster_data, list(allowed_values))
    #     raster_data[mask_invalid] = nodata_value
    #
    #     # Determine which pixel values are actually present (excluding NoData)
    #     present_values = set(np.unique(raster_data)) - {nodata_value}
    #
    #     # Filter lookup table to only include present values
    #     lut = lookup_df[lookup_df[value_column].isin(present_values)].copy()
    #
    #     # Build colormap dictionary: pixel value → (R, G, B)
    #     colormap = {int(r[value_column]): parse_color(r[color_column]) for _, r in lut.iterrows()}
    #     if nodata_value not in colormap:
    #         colormap[nodata_value] = (0, 0, 0)  # Black for NoData
    #
    #     # ---- Write new raster and colormap using rasterio ----
    #     with rasterio.open(output_fp, "w", **meta) as dst:
    #         dst.write(raster_data, 1)
    #         dst.write_colormap(1, colormap)
    #
    #     # ---- Write Raster Attribute Table (RAT) with GDAL ----
    #     ds = gdal.Open(output_fp, gdal.GA_Update)
    #     if ds is None:
    #         raise RuntimeError(f"Failed to open {output_fp} with GDAL for RAT writing.")
    #     band = ds.GetRasterBand(1)
    #
    #     # Prepare sorted lookup for consistent row ordering
    #     lut_sorted = lut.copy()
    #     lut_sorted[value_column] = lut_sorted[value_column].astype(int)
    #     lut_sorted = lut_sorted.sort_values(by=value_column).reset_index(drop=True)
    #
    #     # Create and populate the Raster Attribute Table (RAT)
    #     rat = gdal.RasterAttributeTable()
    #     rat.CreateColumn("Value", gdal.GFT_Integer, gdal.GFU_Generic)
    #     rat.CreateColumn("Label", gdal.GFT_String, gdal.GFU_Name)
    #     rat.CreateColumn("Red", gdal.GFT_Integer, gdal.GFU_Red)
    #     rat.CreateColumn("Green", gdal.GFT_Integer, gdal.GFU_Green)
    #     rat.CreateColumn("Blue", gdal.GFT_Integer, gdal.GFU_Blue)
    #     rat.SetRowCount(len(lut_sorted))
    #
    #     for i, (_, row) in enumerate(lut_sorted.iterrows()):
    #         val = int(row[value_column])
    #         r, g, b = parse_color(row[color_column])
    #         label = str(row[class_column]) if pd.notnull(row[class_column]) else f"Class {val}"
    #         rat.SetValueAsInt(i, 0, val)
    #         rat.SetValueAsString(i, 1, label)
    #         rat.SetValueAsInt(i, 2, r)
    #         rat.SetValueAsInt(i, 3, g)
    #         rat.SetValueAsInt(i, 4, b)
    #
    #     band.SetDefaultRAT(rat)
    #
    #     # ---- Set category names (for QGIS and similar viewers) ----
    #     max_val = max([nodata_value] + list(present_values)) if present_values else nodata_value
    #     size = max(max_val + 1, 256)
    #     cat_names = [""] * size
    #     if nodata_value < size:
    #         cat_names[nodata_value] = "NoData"
    #
    #     for _, row in lut_sorted.iterrows():
    #         v = int(row[value_column])
    #         if v < size:
    #             cat_names[v] = str(row[class_column]) if pd.notnull(row[class_column]) else f"Class {v}"
    #
    #     try:
    #         band.SetCategoryNames(cat_names)
    #     except Exception:
    #         # Some formats may not support category names; ignore safely
    #         pass
    #
    #     ds.FlushCache()
    #     # ds = None

    def get_all_band_names(self):
        """
        Get names of all bands in the dataset.

        Falls back to 'Band {n}' when a band description is missing.

        :return: List of band names.
        """
        if self.dataset is None:
            raise ValueError("Dataset is not set.")

        band_names = []

        for band_no in range(1, self.dataset.count + 1):
            if (
                    self.dataset.descriptions
                    and len(self.dataset.descriptions) >= band_no
            ):
                name = self.dataset.descriptions[band_no - 1]

                if name and name.strip():
                    band_names.append(name)
                    continue

            band_names.append(f"Band {band_no}")

        return band_names

    def get_band_name(self, band_no: int):
        """
        Get the name of a band. Falls back to 'Band {band_no}' if no name is available.

        :param band_no: The band number (1-based index).
        :return: The band name as a string.
        """
        if self.dataset is None:
            raise ValueError("Dataset is not set.")

        if self.dataset.descriptions and len(self.dataset.descriptions) >= band_no:
            name = self.dataset.descriptions[
                band_no - 1]  # Rasterio is 1-based indexing for bands, but descriptions list is 0-based
            if name and name.strip():
                return name

        return f"Band {band_no}"

    def get_band_summaries(self) -> pd.DataFrame:
        """
        Get summaries of all bands in the dataset.

        :return: A DataFrame containing the summaries.
        """
        summaries = {}
        for i in range(1, self.get_spectral_resolution() + 1):
            band_name = self.get_band_name(i)
            data = self.get_data_array(i)
            no_data = self.get_nodata_value()
            summary = BandProcess.get_summary_data(data, nodata=no_data)
            summaries[band_name] = summary
        return pd.DataFrame(summaries).T

    def resample(
            self,
            target_resolution_m: float,
            resampling: ResampleMethod = "nearest",
            in_place: bool = False
    ) -> 'RioRaster':
        """
        Resample the raster to a new spatial resolution specified in meters.

        This keeps the current CRS and geographic extent, but changes the pixel size
        (and thus the raster width/height). If the CRS is geographic (degrees), the
        target meter resolution is converted to degrees using an approximation at the
        raster's center latitude.

        Args:
            target_resolution_m (float): Desired pixel size in meters (square pixels).
            resampling (str): Resampling method name. Options include:
                "nearest", "bilinear", "cubic", "average", "mode", "max", "min",
                "med", "q1", "q3", "lanczos"
            in_place (bool): If True, replace this object's dataset; otherwise return a new RioRaster.

        Returns:
            RioRaster: The resampled raster (or self if in_place=True).
        """
        src = self.dataset
        if src is None:
            raise ValueError("Raster dataset is empty.")

        from rasterio.warp import reproject, Resampling
        if not hasattr(Resampling, resampling):
            raise ValueError(f"Unsupported resampling '{resampling}'.")
        resampling_enum = getattr(Resampling, resampling)

        src = self.dataset
        crs = CRS.from_user_input(src.crs)
        bounds = src.bounds

        # Current extent in CRS units
        width_crs = bounds.right - bounds.left
        height_crs = bounds.top - bounds.bottom

        # Determine target pixel size in *CRS units*
        if crs.is_geographic:
            # Convert meters -> degrees at center latitude
            center_lat = (bounds.top + bounds.bottom) / 2.0
            meters_per_degree_lon = 111320.0 * math.cos(math.radians(center_lat))
            meters_per_degree_lat = 110540.0  # average
            # Avoid division by zero near the poles
            meters_per_degree_lon = max(meters_per_degree_lon, 1e-6)

            xres_units = target_resolution_m / meters_per_degree_lon
            yres_units = target_resolution_m / meters_per_degree_lat
        else:
            # Projected CRS assumed metric
            xres_units = float(target_resolution_m)
            yres_units = float(target_resolution_m)

        # Compute new raster size
        new_width = max(1, int(math.ceil(width_crs / xres_units)))
        new_height = max(1, int(math.ceil(height_crs / yres_units)))

        # Build new transform from existing bounds
        new_transform = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top,
                                    new_width, new_height)

        # Prepare output profile
        out_meta = src.meta.copy()
        out_meta.update({
            "width": new_width,
            "height": new_height,
            "transform": new_transform,
            "crs": src.crs,
        })
        # Keep nodata if present
        if src.nodata is not None:
            out_meta["nodata"] = src.nodata

        memfile = rio.MemoryFile()
        with memfile.open(**out_meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rio.band(src, i),
                    destination=rio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=new_transform,
                    dst_crs=src.crs,
                    resampling=resampling_enum
                )

            # Preserve band descriptions if available
            descriptions = src.descriptions
            if descriptions:
                for i, desc in enumerate(descriptions):
                    if desc:
                        dst.set_band_description(i + 1, desc)

        new_ds = memfile.open()
        if in_place:
            self.dataset = new_ds
            return self
        else:
            return RioRaster(new_ds)

    from rasterio.transform import rowcol

    def get_pixel_value(
            self,
            x: float,
            y: float,
            band: int = 1,
            default=None
    ):
        """
        Get the pixel value at a given map coordinate (x, y).

        Parameters
        ----------
        x : float
            X coordinate (same CRS as the raster)
        y : float
            Y coordinate (same CRS as the raster)
        band : int, optional
            Raster band to read from (default=1)
        default : any, optional
            Value to return if point is outside raster bounds or nodata.
            If None, nodata value from raster is returned.

        Returns
        -------
        value : number or None
            Pixel value at that location.
        """
        if self.dataset is None:
            raise RuntimeError("Dataset not loaded. Use set_dataset() first.")

        # Convert coordinate → pixel row/col
        try:
            row, col = rowcol(self.dataset.transform, x, y)
        except Exception:
            return default

        # Check bounds
        if row < 0 or col < 0 or row >= self.dataset.height or col >= self.dataset.width:
            return default

        # Read value
        value = self.dataset.read(band)[row, col]

        # Handle nodata
        nodata = self.dataset.nodata
        if nodata is not None and value == nodata:
            return default if default is not None else nodata

        return value

    def make_coincident(
            self, template: "RioRaster", resampling: Union[ResampleMethod, str] = "nearest", in_place: bool = False
    ) -> "RioRaster":
        """
        Reproject/resample this raster so that it matches the grid of a template raster:
        - same CRS
        - same transform
        - same width/height

        Typically used to align rasters for pixel-wise operations.
        """
        self.ensure_proj()
        from rasterio.warp import reproject, Resampling

        if not hasattr(Resampling, resampling):
            raise ValueError(f"Unsupported resampling '{resampling}'.")
        resampling_enum = getattr(Resampling, resampling)

        src = self.dataset
        if src is None:
            raise ValueError("Raster dataset is empty.")

        tmpl_ds = template.get_dataset()
        dst_crs = tmpl_ds.crs
        dst_transform = tmpl_ds.transform
        dst_width = tmpl_ds.width
        dst_height = tmpl_ds.height

        # Start from source meta, but force grid to match template
        out_meta = src.meta.copy()
        out_meta.update({
            "crs": dst_crs,
            "transform": dst_transform,
            "width": dst_width,
            "height": dst_height,
        })
        if src.nodata is not None:
            out_meta["nodata"] = src.nodata

        memfile = rio.MemoryFile()
        with memfile.open(**out_meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rio.band(src, i),
                    destination=rio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=resampling_enum,
                )

            # Preserve band descriptions if available
            descriptions = src.descriptions
            if descriptions:
                for i, desc in enumerate(descriptions):
                    if desc:
                        dst.set_band_description(i + 1, desc)

        new_ds = memfile.open()
        if in_place:
            self.dataset = new_ds
            return self
        else:
            # ✅ PASS THE SOURCE PATH TO THE NEW INSTANCE
            return RioRaster(new_ds, source_file_path=self.get_file_name())

    def ignore_values(
            self, values: Union[int, float, List[Union[int, float]]],
            band: Optional[int] = None, in_place: bool = False
    ) -> "RioRaster":
        """
        Replace given pixel value(s) with NaN.

        - Converts data to float32 if not already floating.
        - Sets the raster nodata to NaN.
        - Can operate on a single band or all bands.

        Parameters
        ----------
        values : int | float | list
            Single value or list of values to turn into NaN.
        band : int, optional
            1-based band index. If None, apply to all bands.
        in_place : bool
            If True, modifies this RioRaster and returns self.
            Otherwise returns a new RioRaster.

        Returns
        -------
        RioRaster
            Raster with specified values set to NaN.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        # Normalize values to a 1D numpy array
        if np.isscalar(values):
            values = [values]
        values = np.array(values)

        # Read data
        if band is None:
            data = self.get_data_array()  # shape: (bands, rows, cols) or (rows, cols)
        else:
            data = self.get_data_array(band)  # shape: (rows, cols)

        # Ensure we always have a 3D array for uniform handling
        squeeze_back = False
        if data.ndim == 2:
            data = data[np.newaxis, ...]  # (1, rows, cols)
            squeeze_back = True

        # Cast to float32 so we can safely assign np.nan
        if not np.issubdtype(data.dtype, np.floating):
            data = data.astype("float32", copy=False)

        # Build mask for any of the given values
        # (np.isin works elementwise on the array)
        mask = np.isin(data, values)
        data[mask] = np.nan

        # Grab band descriptions safely while parent is still open
        current_descriptions = None
        if self.dataset.descriptions:
            if band is None:
                current_descriptions = list(self.dataset.descriptions)
            else:
                # If isolating a single band, pass its corresponding description forward
                if len(self.dataset.descriptions) >= band:
                    current_descriptions = [self.dataset.descriptions[band - 1]]

        # Drop back to 2D shape format if originally a single-band request
        if squeeze_back:
            data = data[0, :, :]

        # Update metadata structural components
        meta_data = self.get_meta().copy()
        meta_data.update({
            "dtype": "float32",
            "nodata": np.nan,
            "count": 1 if squeeze_back else data.shape[0],
            "transform": self.get_geo_transform(),
            "crs": self.get_crs()
        })

        # Build new in-memory dataset assigning descriptions safely inside writing mode
        new_ds = self.rio_dataset_from_array(
            data,
            meta_data,
            descriptions=current_descriptions
        )

        new_raster = RioRaster(new_ds, source_file_path=self.get_file_name())

        if in_place:
            if self.dataset:
                try:
                    self.dataset.close()
                except Exception:
                    pass
            self.dataset = new_raster.dataset
            return self

        return new_raster

    def get_unique_colors(
            self,
            ignore_transparent: bool = True,
            alpha_band_index: int = 3
    ) -> pd.DataFrame:
        """
        Return all unique RGB colors in the raster (across all pixels),
        with their occurrence counts.

        - Works for 3-band (RGB) or 4-band (RGBA) rasters.
        - For 4-band rasters, alpha is used only optionally to filter
          fully transparent pixels, then dropped before uniqueness.

        Parameters
        ----------
        ignore_transparent : bool
            If True and 4-band raster, drop pixels where alpha == 0.
        alpha_band_index : int
            Zero-based index of alpha band in the array (default: 3 -> RGBA).

        Returns
        -------
        pandas.DataFrame
            Columns:
                - R, G, B
                - count
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")

        data = self.get_data_array()  # (bands, rows, cols)
        if data.ndim != 3:
            raise ValueError(f"Expected 3D array (bands, rows, cols), got shape {data.shape}")

        bands, rows, cols = data.shape
        if bands not in (3, 4):
            raise ValueError(f"Expected 3 or 4 bands (RGB/RGBA), got {bands}")

        # (rows, cols, bands)
        pixels = np.moveaxis(data, 0, -1).reshape(-1, bands)

        if bands == 4:
            # Optionally drop fully transparent pixels
            if ignore_transparent:
                alpha = pixels[:, alpha_band_index]
                pixels = pixels[alpha != 0]

            # Now drop alpha completely: only keep RGB
            rgb_pixels = np.delete(pixels, alpha_band_index, axis=1)  # shape: (N, 3)
        else:
            rgb_pixels = pixels  # already RGB

        unique_colors, counts = np.unique(rgb_pixels, axis=0, return_counts=True)

        df = pd.DataFrame(unique_colors, columns=["R", "G", "B"])
        df["count"] = counts
        return df

    def close(self):
        if self.dataset:
            self.dataset.close()

    def get_file_name(self):
        # If we explicitly know the real disk file origin, return it first!
        if self.source_file_path and os.path.exists(self.source_file_path):
            return self.source_file_path
        return self.dataset.name if self.dataset else None

    def zonal_stats(
            self,
            zones_gdf: gpd.GeoDataFrame,
            *,
            zone_id_field: str,
            lookup_table: Optional[Union[pd.DataFrame, Dict[int, str], Literal["auto"]]] = "auto",
            stats: Sequence[str] = ("mean", "median", "std", "min", "max", "cv"),
            all_touched: bool = True,
            area_units: Literal["pixels", "meters", "kilometers"] = "kilometers",
            matrix_value_column: Literal["Count", "Area", "percentage"] = "Area",
            matrix_round_digits: int = 2,
            matrix_title: Optional[str] = None,
            log_summary: bool = True,
            percentage_normalize: Literal["row", "column", "grand"] = "row",
            out_xlsx: Optional[Union[str, Path]] = None,
    ) -> pd.DataFrame:
        """
        Unified zonal statistics engine for both numerical and categorical rasters.

        Automatically determines whether to run in categorical matrix mode or
        continuous descriptive stats mode by analyzing the lookup_table param or
        inspecting the raster's internal metadata profiles.

        Parameters
        ----------
        zones_gdf : GeoDataFrame
            Vector polygons representing zones/catchments.
        zone_id_field : str
            Column name in `zones_gdf` containing unique zone identifiers.
        lookup_table : DataFrame, Dict, or "auto", optional, default="auto"
            - If a DataFrame/Dict is provided: Activates categorical matrix mode using those rules.
            - If None: Explicitly bypasses categorization, activating continuous numerical mode.
            - If "auto": Evaluates internal colormaps/RATs to pull a lookup dataframe. Falls
              back to numerical mode if no color schema or category strings exist.
        stats : Sequence[str], default=("mean", "median", "std", "min", "max", "cv")
            Numerical descriptive statistics to compute (continuous mode only).
        all_touched : bool, default=True
            Include all pixels touched by the polygon geometry.
        area_units : 'pixels' | 'meters' | 'kilometers', default='kilometers'
            Unit of scale output for categorical class composition.
        matrix_value_column : 'Count' | 'Area' | 'percentage', default='Area'
            Metric to populate matrix cells with (categorical mode only).
        matrix_round_digits : int, default=2
            Decimal rounding precision for the final table text outputs.
        matrix_title : str, optional
            Title header parsed to the logger wrapper. If None, auto-generates a descriptive title.
        log_summary : bool, default=True
            If true, logs the formatted table automatically using `da_logger`.
        percentage_normalize: "row" | "column" | "grand"
        out_xlsx : str or Path, optional
            File path to export the resulting breakdown to an Excel spreadsheet.
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is not open.")

        # Delay local execution block imports to neutralize module circularity
        from digitalarzengine.processing.raster.rio_process import RioProcess

        resolved_lut = None

        # --- Automatic Mode Evaluation ---
        # if lookup_table == "auto":
        #     # Attempt to pull embedded attributes using your optimized method
        #     try:
        #         # If it has a colormap, RAT, or Category Names, this returns a valid populated DataFrame
        #         potential_lut = self.get_lookup_table()
        #
        #         # Check if the lookup table returned real colors/names, or if it just generated uncolored fallback indexes.
        #         # If it's a raw continuous raster, get_lookup_table returns plain white "#ffffff" placeholders.
        #         has_colors = not potential_lut.empty and not (potential_lut["color"] == "#ffffff").all()
        #
        #         if has_colors:
        #             resolved_lut = potential_lut
        #             da_logger.debug("Auto-detected a categorical layout. Switching to matrix profiling mode.")
        #         else:
        #             resolved_lut = None
        #             da_logger.debug("No distinct categorical definitions found. Defaulting to numerical stats mode.")
        #     except Exception:
        #         resolved_lut = None
        # elif isinstance(lookup_table, (pd.DataFrame, dict)):
        #     resolved_lut = lookup_table

        resolved_lut = None

        # --- Automatic Mode Evaluation ---
        if isinstance(lookup_table, str) and lookup_table == "auto":
            try:
                potential_lut = self.get_lookup_table()

                has_colors = (
                        not potential_lut.empty
                        and not (potential_lut["color"] == "#ffffff").all()
                )

                if has_colors:
                    resolved_lut = potential_lut
                    da_logger.debug(
                        "Auto-detected a categorical layout. Switching to matrix profiling mode."
                    )
                else:
                    resolved_lut = None
                    da_logger.debug(
                        "No distinct categorical definitions found. Defaulting to numerical stats mode."
                    )

            except Exception:
                resolved_lut = None

        elif isinstance(lookup_table, (pd.DataFrame, dict)):
            resolved_lut = lookup_table

        elif lookup_table is None:
            resolved_lut = None

        else:
            raise ValueError("lookup_table must be 'auto', None, DataFrame, or dict.")

        # --- Execution Fork 1: Categorical Matrix Mode ---
         # --- Execution Fork 1: Categorical Matrix Mode ---
        if resolved_lut is not None:
            title = matrix_title or f"Zonal Composition Matrix ({self.get_band_name(1)})"

            df_long = RioProcess.zonal_stats_by_lookup_table(
                raster=self,
                zones_gdf=zones_gdf,
                zone_id_field=zone_id_field,
                lookup_table=resolved_lut,
                value_col="pixel_value",
                label_col="class_name",
                all_touched=all_touched,
                area_units=area_units,
                out_xlsx=out_xlsx if matrix_value_column != "percentage" else None
            )

            # 🚀 FIX: Merge/Aggregate df_long rows if duplicate Zone + Class combinations exist
            if not df_long.empty:
                da_logger.debug("Aggregating long-form data to combine duplicate zone records...")
                df_long = df_long.groupby(["Zone", "Class_Value", "Class_Name"], as_index=False).agg({
                    "Count": "sum",
                    "Area": "sum"
                })

            # Now .pivot() inside generate_zonal_matrix will never throw a ValueError!
            df_matrix = RioProcess.generate_zonal_matrix(
                df_long=df_long,
                value_column=matrix_value_column,
                fill_value=0.0,
                round_digits=matrix_round_digits,
                log_summary=log_summary,
                matrix_title=title,
                normalize=percentage_normalize
            )

            if out_xlsx and not df_matrix.empty:
                out_xlsx = Path(out_xlsx)
                out_xlsx.parent.mkdir(parents=True, exist_ok=True)

                # Save the dataframe normally
                df_matrix.to_excel(out_xlsx, index=False)

                # ✅ Auto-adjust column widths to prevent text clipping
                try:
                    from openpyxl import load_workbook

                    wb = load_workbook(out_xlsx)
                    ws = wb.active
                    for col in ws.columns:
                        # Find max length of contents in the column
                        max_len = max(len(str(cell.value or '')) for cell in col)
                        col_letter = col[0].column_letter
                        # Set adjusted width with a bit of extra padding
                        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

                    wb.save(out_xlsx)
                    wb.close()
                except Exception as e:
                    da_logger.warning(f"Could not auto-adjust Excel column widths: {e}")

            return df_matrix

        # --- Execution Fork 2: Standard Numerical Stats Mode ---
        else:
            title = matrix_title or "Continuous Zonal Summary Statistics"

            df_numerical = RioProcess.zonal_stats_by_band(
                raster=self,
                zones_gdf=zones_gdf,
                zone_id_field=zone_id_field,
                stats=stats,
                all_touched=all_touched,
                out_xlsx=out_xlsx,
                return_format="dataframe"
            )

            if log_summary:
                band_dfs = {
                    layer: df_numerical[df_numerical["Layer"] == layer]
                    for layer in df_numerical["Layer"].unique()
                }
                RioProcess.log_zonal_stats_table(
                    zonal_stats=band_dfs,
                    zone_col=zone_id_field,
                    stats=stats,
                    round_digits=matrix_round_digits,
                    title=title
                )

            return df_numerical

    def plot_raster(
            self,
            output_fp: Union[str, Path],
            title: Optional[str] = None,
            *,
            lookup_table: Optional[Union[pd.DataFrame, Dict[int, str], Literal["auto", None]]] = None,
            gdf: Optional[GeoDataFrame] = None,
            edgecolor: str = "green",
            linewidth: float = 1.0,
            band_names: Optional[Sequence[Union[int, str]]] = None,
            band_titles: Optional[Sequence[str]] = None,

            cmap: str = 'Blues',
            vmin: Optional[Union[float, str]] = None,
            vmax: Optional[Union[float, str]] = None,

            legend: bool = True,
            legend_loc: str = "lower center",
            legend_ncol: int = 3,
            legend_fontsize: int = 8,
            legend_only_present_classes: bool = True,

            label_field: Optional[str] = None,
            label_style: Optional[Dict[str, Any]] = None,
            label_min_dist_px: int = 20,
            label_max_count: Optional[int] = None,
            label_only_largest: bool = True,
            clip_raster: bool = True
    ) -> Union[str, Path]:
        """Generates and saves a high-quality visual plot of the raster bands.

        Supports automatic layout sub-gridding for multiple bands, vector boundary
        overlays with decluttered labels, on-the-fly AOI geometric clipping, and
        categorical raster maps using discrete attribute lookup tables (LUT).

        Args:
            output_fp (str | Path): Destination file path for the output image
                (e.g., 'plots/agri_intensity.png').
            title (str, optional): Main title string displayed at the top of the
                figure canvas.
            lookup_table (DataFrame | dict | 'auto' | None, default='auto'):
                Classification mapping rules. If 'auto', inspects internal colormaps
                or Raster Attribute Tables (RAT). Pass None to force continuous
                grayscale or multi-band RGB collage pipelines.
            gdf (GeoDataFrame, optional): Vector layers (e.g., administrative boundaries)
                to overlay on top of the raster imagery. Auto-reprojects if CRS mismatches.
            edgecolor (str, default='green'): Line boundary color for the GDF overlay.
            linewidth (float, default=1.0): Stroke weight thickness for the vector lines.
            band_names (sequence of int/str, optional): Sequence of 1-based indices or
                string description keys defining which specific bands to plot.
            band_titles (sequence of str, optional): Explicit text strings to assign
                as individual subplot sub-headers (replaces default 'Band N' strings).
            cmap (str, default='Blues'): Matplotlib colormap name string for single-band plots.
            vmin (float | str, optional): Minimum color stretching range limit. Supports
                absolute float scalars or local percentile strings (e.g., '2%').
            vmax (float | str, optional): Maximum color stretching range limit. Supports
                absolute float scalars or local percentile strings (e.g., '98%').
            legend (bool, default=True): If True, renders a discrete categorical color
                legend layout at the bottom of the map canvas.
            legend_loc (str, default='lower center'): Boundary anchor location for the legend box.
            legend_ncol (int, default=3): Max item column width layout constraints for the legend.
            legend_fontsize (int, default=8): Label typography font sizing inside the legend box.
            legend_only_present_classes (bool, default=True): Cleans discrete legends
                by dropping classes completely absent from the current data array cluster.
            label_field (str, optional): Target text string column inside the `gdf` to use
                for creating geometric map feature annotations.
            label_style (dict, optional): Typography dictionary overrides parsed directly
                to `ax.text()` (e.g., font sizing, weight, alpha opacity backgrounds).
            label_min_dist_px (int, default=20): Pixel-space threshold boundary limits for the
                declutter engine. Prevents textual layout clipping on dense layers.
            label_max_count (int, optional): Strict upper bound threshold capping total feature
                labels rendered on a single grid window.
            label_only_largest (bool, default=True): Sorts spatial geometries downward by area size,
                guaranteeing major spatial zones get labeled first when label caps are hit.
            clip_raster (bool, default=True): If True and a valid `gdf` is provided, clips
                the underlying array footprint **in-place** to the total bounds envelope of the
                AOI vector geometries before calling plotting matrix transformations.

        Returns:
            str | Path: The exact path where the output visualization map was written.

        Raises:
            ValueError: If the underlying raster dataset structure is uninitialized or empty.

        Notes:
            Setting `clip_raster=True` modifies the current `RioRaster` instance dataset object
            mutably in-place via an immediate masking call. If you need to keep un-cropped matrix
            pixels intact for downstream full-grid model computations, make a copy or execute
            clipping exclusively at the end of the script pipeline.

        Examples:
            >>> # Case 1: Simple Continuous Normalized Raster with Governorate Overlay
            >>> r = RioRaster("data/output/agricultural_intensity.tif")
            >>> gov_gdf = get_gov_gdf()
            >>> r.plot_raster(
            ...     output_fp="plots/agri_intensity.png",
            ...     title="Agricultural Intensity Distribution",
            ...     lookup_table=None, # continuous stretch
            ...     gdf=gov_gdf,
            ...     label_field="NAME_E",
            ...     cmap="viridis",
            ...     vmin="2%",
            ...     vmax="98%"
            ... )

            >>> # Case 2: Categorical Layout Map with Custom Classification Legend
            >>> r_classes = RioRaster("data/output/aez_zones.tif")
            >>> r_classes.plot_raster(
            ...     output_fp="plots/aez_zones.png",
            ...     title="Agro-Ecological Classification",
            ...     lookup_table="auto", # parse RAT or embedded colormap structures
            ...     gdf=gov_gdf,
            ...     edgecolor="black"
            ... )
        """
        if self.dataset is None:
            raise ValueError("Raster dataset is empty.")
        if clip_raster:
            self.clip_raster(gdf, in_place=True)
        if lookup_table is not None:

            resolved_lut = None

            if isinstance(lookup_table, str) and lookup_table == "auto":
                try:
                    potential_lut = self.get_lookup_table()

                    has_valid_lut = (
                            potential_lut is not None
                            and not potential_lut.empty
                            and "pixel_value" in potential_lut.columns
                            and "color" in potential_lut.columns
                            and "class_name" in potential_lut.columns
                    )

                    if has_valid_lut:
                        resolved_lut = potential_lut
                        da_logger.debug("Auto-detected categorical raster.")
                    else:
                        resolved_lut = None
                        da_logger.debug("No valid lookup table found. Plotting simple raster.")

                except Exception as ex:
                    resolved_lut = None
                    da_logger.debug(
                        f"Lookup table detection failed. Plotting simple raster. Error: {ex}"
                    )

            elif isinstance(lookup_table, (pd.DataFrame, dict)):
                resolved_lut = lookup_table

            elif lookup_table is None:
                resolved_lut = None

            else:
                raise ValueError("lookup_table must be 'auto', None, DataFrame, or dict.")

            if resolved_lut is not None:
                from digitalarzengine.processing.raster.rio_process import RioProcess
                return RioProcess.plot_categorical_raster(
                    raster=self,
                    title=title or self.get_band_name(1),
                    output_fp=output_fp,
                    lookup_table=resolved_lut,
                    gdf=gdf,
                    edgecolor=edgecolor if edgecolor != "green" else "black",
                    linewidth=linewidth,
                    band_names=band_names,
                    band_titles=band_titles,
                    legend=legend,
                    legend_ncol=legend_ncol,
                    legend_fontsize=legend_fontsize,
                    legend_only_present_classes=legend_only_present_classes,
                    label_field=label_field,
                    label_style=label_style,
                    label_min_dist_px=label_min_dist_px,
                    label_max_count=label_max_count,
                    label_only_largest=label_only_largest
                )

            # Simple RGB / grayscale plot
            output_fp = Path(output_fp)
            output_fp.parent.mkdir(parents=True, exist_ok=True)

            ds = self.dataset
            extent = [
                ds.bounds.left,
                ds.bounds.right,
                ds.bounds.bottom,
                ds.bounds.top,
            ]

            fig, ax = plt.subplots(figsize=(10, 10))

            if ds.count >= 3:
                arr = ds.read([1, 2, 3])
                img = np.moveaxis(arr, 0, -1)

                if img.dtype != np.uint8:
                    img = img.astype("float32")
                    p2, p98 = np.nanpercentile(img, [2, 98])
                    img = np.clip((img - p2) / max(p98 - p2, 1e-9), 0, 1)

                ax.imshow(img, extent=extent)

            else:
                arr = ds.read(1).astype("float32")

                if ds.nodata is not None:
                    arr[arr == ds.nodata] = np.nan

                p2, p98 = np.nanpercentile(arr, [2, 98])
                img = np.clip((arr - p2) / max(p98 - p2, 1e-9), 0, 1)

                ax.imshow(img, extent=extent, cmap="gray")

            if gdf is not None and not gdf.empty:
                if gdf.crs is not None and ds.crs is not None and gdf.crs != ds.crs:
                    gdf = gdf.to_crs(ds.crs)

                gdf.boundary.plot(
                    ax=ax,
                    edgecolor=edgecolor,
                    linewidth=linewidth
                )

                if label_field and label_field in gdf.columns:
                    style = {
                        "fontsize": 8,
                        "color": edgecolor,
                        "ha": "center",
                        "va": "center",
                    }

                    if label_style:
                        style.update(label_style)

                    for _, row in gdf.iterrows():
                        if row.geometry is None or row.geometry.is_empty:
                            continue

                        p = row.geometry.representative_point()
                        ax.text(p.x, p.y, str(row[label_field]), **style)

            if title:
                ax.set_title(title)

            ax.set_axis_off()
            plt.tight_layout()
            fig.savefig(output_fp, dpi=300, bbox_inches="tight", pad_inches=0.05)
            plt.close(fig)
        else:
            from digitalarzengine.processing.raster.rio_process import RioProcess
            RioProcess.create_collage_images(self,
                                             title=title,
                                             output_fp=output_fp,
                                             no_of_rows=1,
                                             cmap=cmap,
                                             edgecolor=edgecolor,
                                             linewidth=linewidth,
                                             gdf=gdf,
                                             vmin=vmin,
                                             vmax=vmax,

                                             band_names=band_names,
                                             band_titles=band_titles,

                                             label_field=label_field,  # e.g. "name" / "id"
                                             label_style=label_style,  # matplotlib text kwargs

                                             label_min_dist_px=label_min_dist_px,  # declutter threshold (pixels)
                                             label_max_count=label_max_count,  # cap labels per subplot
                                             label_only_largest=label_only_largest,  # label largest polys first
                                             )

        return output_fp

    # def plot_raster(
    #         self,
    #         output_fp: Union[str, Path],
    #         title: Optional[str] = None,
    #         *,
    #         lookup_table: Optional[Union[pd.DataFrame, Dict[int, str], Literal["auto", None]]] = "auto",
    #         gdf: Optional[GeoDataFrame] = None,
    #         edgecolor: str = "green",
    #         linewidth: float = 1.0,
    #         band_names: Optional[Sequence[Union[int, str]]] = None,
    #         band_titles: Optional[Sequence[str]] = None,
    #
    #         cmap: str = 'Blues',
    #         vmin: Optional[Union[float, str]] = None,
    #         vmax: Optional[Union[float, str]] = None,
    #
    #         legend: bool = True,
    #         legend_loc: str = "lower center",
    #         legend_ncol: int = 3,
    #         legend_fontsize: int = 8,
    #         legend_only_present_classes: bool = True,
    #
    #         label_field: Optional[str] = None,
    #         label_style: Optional[Dict[str, Any]] = None,
    #         label_min_dist_px: int = 20,
    #         label_max_count: Optional[int] = None,
    #         label_only_largest: bool = True,
    #         clip_raster: bool = True
    # ) -> Union[str, Path]:
    #     """Generates and saves a high-quality visual plot of the raster bands."""
    #     if self.dataset is None:
    #         raise ValueError("Raster dataset is empty.")
    #
    #     if clip_raster and gdf is not None and not gdf.empty:
    #         self.clip_raster(gdf, in_place=True)
    #
    #     resolved_lut = None
    #
    #     # --- Evaluate and Resolve Lookup Table ---
    #     if lookup_table is not None:
    #         if isinstance(lookup_table, str) and lookup_table == "auto":
    #             try:
    #                 potential_lut = self.get_lookup_table()
    #                 has_valid_lut = (
    #                         potential_lut is not None
    #                         and not potential_lut.empty
    #                         and "pixel_value" in potential_lut.columns
    #                         and "color" in potential_lut.columns
    #                         and "class_name" in potential_lut.columns
    #                         and not (potential_lut["color"] == "#ffffff").all()
    #                 )
    #                 if has_valid_lut:
    #                     resolved_lut = potential_lut
    #                     da_logger.debug("Auto-detected categorical raster layer.")
    #                 else:
    #                     da_logger.debug("No valid embedded categorical definitions. Falling back to continuous.")
    #             except Exception as ex:
    #                 da_logger.debug(f"Lookup table auto-detection bypassed: {ex}")
    #         elif isinstance(lookup_table, (pd.DataFrame, dict)):
    #             resolved_lut = lookup_table
    #         else:
    #             raise ValueError("lookup_table must be 'auto', None, a DataFrame, or a dict.")
    #
    #     # =========================================================================
    #     # FORK A: Categorical Plotting Pipeline
    #     # =========================================================================
    #     if resolved_lut is not None:
    #         from digitalarzengine.processing.raster.rio_process import RioProcess
    #         return RioProcess.plot_categorical_raster(
    #             raster=self,
    #             title=title or self.get_band_name(1),
    #             output_fp=output_fp,
    #             lookup_table=resolved_lut,
    #             gdf=gdf,
    #             edgecolor=edgecolor if edgecolor != "green" else "black",
    #             linewidth=linewidth,
    #             band_names=band_names,
    #             band_titles=band_titles,
    #             legend=legend,
    #             legend_ncol=legend_ncol,
    #             legend_fontsize=legend_fontsize,
    #             legend_only_present_classes=legend_only_present_classes,
    #             label_field=label_field,
    #             label_style=label_style,
    #             label_min_dist_px=label_min_dist_px,
    #             label_max_count=label_max_count,
    #             label_only_largest=label_only_largest
    #         )
    #
    #     # =========================================================================
    #     # FORK B: Continuous / Multiband Collage Engine Pipeline
    #     # =========================================================================
    #     # Fallback 1: Multi-band or explicit user parameters go to the collage engine
    #     if self.dataset.count > 1 or band_names is not None or vmin is not None or vmax is not None:
    #         from digitalarzengine.processing.raster.rio_process import RioProcess
    #         return RioProcess.create_collage_images(
    #             self,
    #             title=title,
    #             output_fp=output_fp,
    #             no_of_rows=1,
    #             cmap=cmap,
    #             edgecolor=edgecolor,
    #             linewidth=linewidth,
    #             gdf=gdf,
    #             vmin=vmin,
    #             vmax=vmax,
    #             band_names=band_names,
    #             band_titles=band_titles,
    #             label_field=label_field,
    #             label_style=label_style,
    #             label_min_dist_px=label_min_dist_px,
    #             label_max_count=label_max_count,
    #             label_only_largest=label_only_largest,
    #         )
    #
    #     # Fallback 2: Simple standard 1-Band grayscale matplotlib renderer
    #     output_fp = Path(output_fp)
    #     output_fp.parent.mkdir(parents=True, exist_ok=True)
    #
    #     ds = self.dataset
    #     extent = [ds.bounds.left, ds.bounds.right, ds.bounds.bottom, ds.bounds.top]
    #
    #     fig, ax = plt.subplots(figsize=(10, 10))
    #     arr = ds.read(1).astype("float32")
    #
    #     if ds.nodata is not None:
    #         arr[arr == ds.nodata] = np.nan
    #
    #     p2, p98 = np.nanpercentile(arr, [2, 98])
    #     # Protect against division by zero if it is a completely flat raster
    #     denom = max(p98 - p2, 1e-9)
    #     img = np.clip((arr - p2) / denom, 0, 1)
    #
    #     ax.imshow(img, extent=extent, cmap=cmap if cmap else "gray")
    #
    #     if gdf is not None and not gdf.empty:
    #         if gdf.crs is not None and ds.crs is not None and gdf.crs != ds.crs:
    #             gdf = gdf.to_crs(ds.crs)
    #
    #         gdf.boundary.plot(ax=ax, edgecolor=edgecolor, linewidth=linewidth)
    #
    #         if label_field and label_field in gdf.columns:
    #             style = {"fontsize": 8, "color": edgecolor, "ha": "center", "va": "center"}
    #             if label_style:
    #                 style.update(label_style)
    #
    #             for _, row in gdf.iterrows():
    #                 if row.geometry is None or row.geometry.is_empty:
    #                     continue
    #                 p = row.geometry.representative_point()
    #                 ax.text(p.x, p.y, str(row[label_field]), **style)
    #
    #     if title:
    #         ax.set_title(title)
    #
    #     ax.set_axis_off()
    #     plt.tight_layout()
    #     fig.savefig(output_fp, dpi=300, bbox_inches="tight", pad_inches=0.05)
    #     plt.close(fig)
    #
    #     return output_fp


    @staticmethod
    def load_band_as_raster(
            src_path: Union[str, Path],
            band_identifier: Union[int, str]
    ) -> 'RioRaster':
        """
        Static factory method to extract and load a single band directly from a raster file
        path using its 1-based index or its description name.

        Parameters
        ----------
        src_path : str | Path
            Path to the input raster file.
        band_identifier : int | str
            The 1-based index of the band (e.g., 1) or its string name/description.

        Returns
        -------
        RioRaster
            A new single-band RioRaster instance containing the isolated band.
        """
        if isinstance(src_path, Path):
            src_path = str(src_path)

        if not os.path.exists(src_path):
            raise FileNotFoundError(f"Raster file not available at {src_path}")

        # Open the source file context to read metadata and extract the band
        with rasterio.open(src_path, mode='r', ignore_cog_layout_break='YES') as src:
            band_index = None

            # Case 1: Identified by band description name string
            if isinstance(band_identifier, str):
                descriptions = src.descriptions
                if descriptions and band_identifier in descriptions:
                    band_index = descriptions.index(band_identifier) + 1
                else:
                    raise ValueError(
                        f"Band name '{band_identifier}' not found in dataset descriptions: {descriptions}"
                    )

            # Case 2: Identified by 1-based integer index
            elif isinstance(band_identifier, (int, np.integer)):
                if 1 <= band_identifier <= src.count:
                    band_index = int(band_identifier)
                else:
                    raise IndexError(
                        f"Band index {band_identifier} out of bounds. Dataset has {src.count} band(s)."
                    )
            else:
                raise TypeError("band_identifier must be either a string (band name) or an integer (1-based index).")

            # Read target band data (returns a 2D array of shape: rows, cols)
            band_data = src.read(band_index)

            # Handle profile setup safely
            new_meta = src.meta.copy()
            new_meta.update({
                "count": 1,
                "dtype": str(band_data.dtype)
            })

            # Grab original band name or fallback cleanly
            original_name = None
            if src.descriptions and len(src.descriptions) >= band_index:
                name = src.descriptions[band_index - 1]
                if name and name.strip():
                    original_name = name

            if not original_name:
                original_name = f"Band {band_index}"

        # Outside the with-block (src is closed), rebuild into an in-memory DatasetReader
        # Wrap 2D data back to 3D array layout (1, rows, cols) for the generator structure
        band_data_3d = band_data[np.newaxis, ...]

        new_ds = RioRaster.rio_dataset_from_array(
            band_data_3d,
            new_meta,
            descriptions=[original_name]
        )

        return RioRaster(new_ds)
