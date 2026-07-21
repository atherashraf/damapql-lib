from __future__ import annotations

import math
from time import time
from typing import Union, List, Any, Optional, Iterable, Dict, Literal, Tuple

import cv2
import pandas as pd
from numpy import signedinteger
from scipy.spatial import KDTree
import numpy as np
import geopandas as gpd
from shapely.geometry import shape
from rasterio.features import shapes
from affine import Affine



class BandProcess:

    @staticmethod
    def reclassify_band(img_arr: np.array, thresholds: dict, nodata=0) -> np.array:
        """
        img_arr: must be as row, col
        :param thresholds:
            example:  {
                    "water": (('lt', 0.015), 4),
                    "built-up": ((0.015, 0.02), 1),
                    "barren": ((0.07, 0.27), 2),
                    "vegetation": (('gt', 0.27), 3)
                }

        """
        if img_arr.ndim > 2:
            img_arr = np.squeeze(img_arr)
        res = np.empty(img_arr.shape)
        res[:] = nodata
        for key in thresholds:
            if thresholds[key][0][0] == 'lt':
                res = np.where(img_arr <= thresholds[key][0][1], thresholds[key][1], res)
            elif thresholds[key][0][0] == 'gt':
                res = np.where(img_arr >= thresholds[key][0][1], thresholds[key][1], res)
            else:
                con = np.logical_and(img_arr >= thresholds[key][0][0], img_arr <= thresholds[key][0][1])
                res = np.where(con, thresholds[key][1], res)
        return res.astype(np.uint8)

    @staticmethod
    def create_binary_mask(band_data: np.ndarray, class_value, tol: float = 1e-6) -> np.ndarray:
        """
        Binary mask: 1 where band_data equals class_value, else 0.
        Works for ints and floats; ignores NaNs.
        """
        arr = band_data
        if np.issubdtype(arr.dtype, np.floating):
            mask = np.isclose(arr, class_value, atol=tol, equal_nan=False)
        else:
            mask = (arr == class_value)
        # Ensure NaNs never match
        if np.issubdtype(arr.dtype, np.floating):
            mask &= ~np.isnan(arr)
        return mask.astype(np.uint8)

    @staticmethod
    def raster_2_polygon(
            band_data: np.ndarray,
            classes: list | None = None,
            lookup_table: pd.DataFrame | dict | Literal["auto"] | None = None,
            value_column: str = "pixel_value",
            label_column: str = "class_name",
            transform: Affine | None = None,
            crs=None,
            simplify_tol: float = 0.0,
            float_match_tol: float = 1e-6,
            nodata_value: Any = None,
    ) -> gpd.GeoDataFrame:
        """
        Vectorize class regions to polygons using rasterio.features.shapes.

        If 'classes' is None, it extracts target pixel values using the provided
        'lookup_table'. If both are missing, it falls back to unique array values.

        Parameters
        ----------
        band_data : np.ndarray
            2D raster array containing categorical pixel values.
        classes : list | None, default None
            Explicit subset of pixel values to process.
            Example:
                [10, 20, 30]
        lookup_table : pd.DataFrame | dict | str | None, default None
            Structural attribute mapping used to target unique classes and bind labels.
            Supported formats:

            - pandas.DataFrame: Must contain `value_column` and optionally `label_column`.
                ┌─────────────┬─────────────┬─────────┐
                │ pixel_value │ class_name  │ color   │
                ├─────────────┼─────────────┼─────────┤
                │ 1           │ Built-up    │ #ff0000 │
                │ 2           │ Vegetation  │ #00ff00 │
                └─────────────┴─────────────┴─────────┘

            - dictionary: Simple direct mapping of {pixel_value: class_label}.
                {
                    1: "Built-up",
                    2: "Vegetation"
                }

        value_column : str, default "pixel_value"
            The column name in lookup_table DataFrame identifying raw pixel values.
        label_column : str, default "class_name"
            The column name to extract human-readable attributes for the output GeoDataFrame.
        transform : Affine | None, default None
            Affine transform matrix mapping pixel coordinates to geographic coordinates.
        crs : Any, default None
            Coordinate Reference System to assign to the output GeoDataFrame.
        simplify_tol : float, default 0.0
            Distance tolerance parameter passed to shapely's Douglas-Peucker simplification.
        float_match_tol : float, default 1e-6
            Tolerance used for `np.isclose` floating point mask matches.
        nodata_value : Any, default None
            Pixel value representing background NoData to exclude from vectorization.

        Returns
        -------
        gpd.GeoDataFrame
            Polygons wrapping vectorized contiguous class pixels. If a lookup table is used,
            the resulting table incorporates the mapped `label_column` names.
        """
        if band_data.ndim != 2:
            raise ValueError("band_data must be a 2D array (single band).")

        if transform is None:
            transform = Affine.identity()

        # --- Dynamic Class Resolution ---
        class_labels = {}

        if classes is not None:
            # Prioritize explicitly passed classes
            target_classes = list(classes)
        elif lookup_table is not None and not isinstance(lookup_table, str):
            # Extract from passed DataFrame or Dictionary
            if isinstance(lookup_table, pd.DataFrame):
                if value_column in lookup_table.columns:
                    target_classes = lookup_table[value_column].tolist()
                    if label_column in lookup_table.columns:
                        class_labels = dict(zip(lookup_table[value_column], lookup_table[label_column]))
                else:
                    raise ValueError(f"Value column '{value_column}' missing from lookup_table DataFrame.")
            elif isinstance(lookup_table, dict):
                target_classes = list(lookup_table.keys())
                class_labels = {k: str(v) for k, v in lookup_table.items()}
        else:
            # Fallback: Extract unique values directly from the matrix
            if np.issubdtype(band_data.dtype, np.floating):
                unique_vals = np.unique(band_data[~np.isnan(band_data)])
            else:
                unique_vals = np.unique(band_data)
            target_classes = unique_vals.tolist()

        # Explicitly remove NoData values from target list to prevent vectorizing null space
        if nodata_value is not None:
            target_classes = [c for c in target_classes if c != nodata_value]
        elif hasattr(band_data, "fill_value"):  # Handle masked arrays safely
            target_classes = [c for c in target_classes if c != band_data.fill_value]

        records = []

        # --- Vectorization Loop ---
        for class_value in target_classes:
            binary_mask = BandProcess.create_binary_mask(
                band_data, class_value, tol=float_match_tol
            ).astype(np.uint8)

            # If the class doesn't exist in this chunk, skip early
            if not np.any(binary_mask):
                continue

            for geom, val in shapes(
                    binary_mask,
                    mask=binary_mask.astype(bool),
                    transform=transform
            ):
                if val != 1:
                    continue

                poly = shape(geom)
                if poly.is_empty:
                    continue

                if simplify_tol > 0.0:
                    poly = poly.simplify(simplify_tol, preserve_topology=True)
                    if poly.is_empty:
                        continue

                # Prepare feature attribute properties
                record = {
                    "class": class_value,
                    "geometry": poly
                }
                if class_value in class_labels:
                    record[label_column] = class_labels[class_value]

                records.append(record)

        return gpd.GeoDataFrame(records, crs=crs)

    @staticmethod
    def get_summary_data(
            data: np.ndarray,
            nodata=None,
            ignore_negative_values=True,
            ignore_values: list = (),
    ):
        """
        Returns dict with keys: mean, median, std, min, q25, q75, max, sum
        All NaN-safe. nodata and ignore_values are masked with float-safe comparisons.
        """
        data = data.astype(np.float64)  # for NaN ops

        # Float-safe nodata masking
        if nodata is not None:
            if np.issubdtype(data.dtype, np.floating):
                data[np.isclose(data, nodata, rtol=0.0, atol=1e-9)] = np.nan
            else:
                data[data == nodata] = np.nan

        # Mask explicit ignore values
        for value in ignore_values:
            if np.issubdtype(data.dtype, np.floating):
                data[np.isclose(data, value, rtol=0.0, atol=1e-9)] = np.nan
            else:
                data[data == value] = np.nan

        if ignore_negative_values:
            data[data <= 0] = np.nan

        if np.isnan(data).all():
            return {
                "mean": np.nan, "median": np.nan, "std": np.nan,
                "min": np.nan, "q25": np.nan, "q75": np.nan, "max": np.nan, "sum": np.nan
            }

        return {
            "mean": np.nanmean(data),
            "median": np.nanquantile(data, 0.5),
            "std": np.nanstd(data),
            "min": np.nanmin(data),
            "q25": np.nanquantile(data, 0.25),
            "q75": np.nanquantile(data, 0.75),
            "max": np.nanmax(data),
            "sum": np.nansum(data),
        }

    # @classmethod
    # def get_value_area_data(
    #         cls, data: np.ndarray, no_data,
    #         spatial_res, values=None,
    #         value_labels=None,  # dict|list|tuple|callable -> label per value (optional)
    #         unit: str = None,  # text label for the output unit
    # ) -> list:
    #     """
    #     Compute area per pixel value.
    #
    #     Parameters
    #     ----------
    #     data : np.ndarray
    #         Raster array.
    #     no_data : any
    #         No-data value.
    #     spatial_res : tuple|number
    #         (res_x, res_y) or a single area-per-pixel value (already in squared units).
    #         If tuple/list of length 2 -> area_per_pixel = res_x * res_y
    #         Else -> area_per_pixel = spatial_res (assumed already an area or a side length if you intend so).
    #     values : list, optional
    #         Subset of pixel values to include (passed through to get_value_count_data).
    #     value_labels : dict|list|tuple|callable, optional
    #         - dict: {value: "label"}
    #         - list/tuple: aligned with `values` (same order)
    #         - callable: f(value) -> "label"
    #     unit : str
    #         Output unit label (e.g., "sq. m", "sq. km", "sq. units").
    #
    #     Returns
    #     -------
    #     list[dict]
    #         Each item: {"value": <val>, "area": <float>, "unit": <str>, "label": <str?>}
    #     """
    #     value_count = cls.get_value_count_data(data, no_data, values)
    #     out = []
    #
    #     # Determine area per pixel in the SAME units as spatial_res
    #     if isinstance(spatial_res, (list, tuple)) and len(spatial_res) == 2:
    #         area_per_pixel = spatial_res[0] * spatial_res[1]
    #     else:
    #         area_per_pixel = spatial_res
    #
    #     # Helper to resolve label
    #     def resolve_label(val):
    #         da_logger.debug(f"value {val}")
    #         if value_labels is None:
    #             return None
    #         if callable(value_labels):
    #             return value_labels(val)
    #         if isinstance(value_labels, dict):
    #             return value_labels.get(val)
    #         if isinstance(value_labels, (list, tuple)) and values is not None:
    #             try:
    #                 v = int(val)
    #                 return value_labels[v] if v < len(value_labels) else None
    #             except ValueError:
    #                 return None
    #         return None
    #
    #     for v in value_count:
    #         val = v["value"]
    #         item = {
    #             "value": val,
    #             "area": v["count"] * area_per_pixel,
    #         }
    #         if unit is not None: item["unit"] = unit
    #         lbl = resolve_label(val)
    #         if lbl is not None:
    #             item["label"] = lbl
    #         out.append(item)
    #
    #     return out

    @classmethod
    def get_value_area_data(
            cls,
            data: np.ndarray,
            no_data: Any,
            spatial_res: Union[tuple, list, float, int],
            values: Optional[Iterable] = None,
            pixel_classes: Optional[Dict[int, str]] = None,  # {10:"Tree cover", ...}
            crs_units: Optional[str] = None,  # "m" or "degree" (recommended to pass from raster CRS)
            unit: Optional[str] = None,  # override
            to_km2: bool = False,  # only valid when unit is m2
            include_percent: bool = False,
            return_format: str = "df",  # "df" | "list"
            allow_degree_area: bool = False,  # if CRS is degrees, block by default
    ):

        """
        Compute area statistics per raster pixel value.

        This method aggregates pixel counts by unique raster values and converts
        them into physical area using the spatial resolution of the raster.
        It is designed for categorical rasters (e.g., land cover, soil classes)
        where pixel values represent discrete classes.

        Area units are inferred from the spatial resolution and CRS units unless
        explicitly overridden.

        Parameters
        ----------
        data : np.ndarray
            2D raster array containing categorical pixel values.
        no_data : Any
            Pixel value representing no-data, which will be excluded from
            the computation.
        spatial_res : tuple, list, float, or int
            Spatial resolution of the raster:
            - (res_x, res_y): pixel size in CRS units (e.g., meters or degrees).
            - scalar: assumed to already represent area per pixel in squared units.
        values : iterable, optional
            Subset of pixel values to include. If None, all valid values are used.
        pixel_classes : dict[int, str], optional
            Mapping of pixel value to human-readable class name
            (e.g., {40: "Cropland", 60: "Bare land"}). If provided, a
            ``class_name`` column will be added.
        crs_units : {"m", "meter", "meters", "degree", "degrees"}, optional
            Units of the raster CRS. Used to infer area units when
            ``spatial_res`` is a (res_x, res_y) tuple.
            If omitted, units are marked as unknown.
        unit : str, optional
            Explicit unit label for the computed area (e.g., "m2").
            Overrides inferred units if provided.
        to_km2 : bool, default False
            If True, converts area from square meters to square kilometers
            and adds an ``area_km2`` column. Only valid when area units are meters.
        include_percent : bool, default False
            If True, adds a ``percent`` column representing the percentage
            contribution of each class relative to the total area.
        return_format : {"df", "list"}, default "df"
            Output format:
            - "df": return a pandas DataFrame.
            - "list": return a list of dictionaries (legacy-compatible).
        allow_degree_area : bool, default False
            If False (default), raises an error when attempting to compute
            area in degrees squared (deg²), as this is not physically meaningful.
            Set to True only if degree-based area is explicitly intended.

        Returns
        -------
        pandas.DataFrame or list[dict]
            Area statistics per pixel value. When returned as a DataFrame,
            columns may include:
            - value : int or Any
                Pixel value.
            - count : float
                Number of pixels of this value.
            - area : float
                Total area for the value in inferred or specified units.
            - unit : str
                Area unit label.
            - class_name : str, optional
                Human-readable class name (if ``pixel_classes`` is provided).
            - area_km2 : float, optional
                Area in square kilometers (if ``to_km2=True``).
            - percent : float, optional
                Percentage of total area (if ``include_percent=True``).

        Notes
        -----
        - For rasters in geographic CRS (e.g., EPSG:4326), area computation
          in degrees squared is not recommended. Reproject the raster to a
          projected CRS (e.g., UTM) or compute area using pixel-area methods
          (e.g., Google Earth Engine ``pixelArea()``).
        - For categorical rasters, reprojection should use nearest-neighbor
          resampling to avoid class mixing.

        Examples
        --------
        >>> df = BandProcess.get_value_area_data(
        ...     data,
        ...     no_data=0,
        ...     spatial_res=(10, 10),
        ...     crs_units="m",
        ...     pixel_classes={40: "Cropland", 60: "Bare land"},
        ...     to_km2=True,
        ...     include_percent=True
        ... )
        """

        value_count = cls.get_value_count_data(data, no_data, values)
        if not value_count:
            df_empty = pd.DataFrame(columns=["value", "count", "area"])
            return df_empty if return_format == "df" else []

        # Determine area_per_pixel
        inferred_unit = None
        if isinstance(spatial_res, (list, tuple)) and len(spatial_res) == 2:
            xres, yres = float(spatial_res[0]), float(spatial_res[1])
            area_per_pixel = abs(xres * yres)

            # infer unit from crs_units (best) or guess
            if unit is None:
                if crs_units is None:
                    inferred_unit = "unknown2"
                elif crs_units.lower() in ["m", "meter", "meters"]:
                    inferred_unit = "m2"
                elif crs_units.lower() in ["degree", "degrees", "deg"]:
                    inferred_unit = "deg2"
                else:
                    inferred_unit = "unknown2"
            else:
                inferred_unit = unit
        else:
            # spatial_res is assumed to be already an area-per-pixel scalar
            area_per_pixel = float(spatial_res)
            inferred_unit = unit or "pixel_area_units"

        # Block degree-based area unless explicitly allowed
        if (inferred_unit == "deg2") and (not allow_degree_area):
            raise ValueError(
                "Area computation in degrees^2 is not meaningful. "
                "Reproject raster to a metric CRS (e.g., UTM) or compute area using GEE pixelArea()."
            )

        # Build DataFrame
        df = pd.DataFrame(value_count)  # [{"value":..,"count":..}]
        df["value"] = df["value"].apply(lambda x: int(x) if str(x).isdigit() else x)
        df["count"] = df["count"].astype(float)
        df["area"] = df["count"] * area_per_pixel
        df["unit"] = inferred_unit

        # optional class names
        if pixel_classes is not None:
            df["class_name"] = df["value"].apply(
                lambda v: pixel_classes.get(v, "Unknown") if isinstance(v, int) else "Unknown"
            )

        # optional km2 conversion only if m2
        if to_km2:
            if inferred_unit != "m2":
                raise ValueError("to_km2=True requires area unit to be m2.")
            df["area_km2"] = df["area"] / 1e6

        if include_percent:
            total = df["area"].sum()
            df["percent"] = (df["area"] / total * 100) if total > 0 else 0.0

        try:
            df = df.sort_values("value").reset_index(drop=True)
        except Exception:
            df = df.reset_index(drop=True)

        return df if return_format == "df" else df.to_dict("records")

    @staticmethod
    def get_value_count_data(
            data: np.ndarray,
            no_data,
            values=None,
            value_labels=None  # optional label mapping
    ) -> list:
        """
        :param data: np.ndarray, e.g., data = raster.get_data_array(band_no)
        :param no_data: No-data value from raster
        :param values: list of pixel values or ranges; if None, values are auto-detected
        :param value_labels: dict|list|tuple|callable
            - dict: {value: "label"}
            - list/tuple: aligned with `values` order
            - callable: f(value) -> "label"
        :return: list of dicts like {"value": <val/range>, "count": <int>, "label": <label?>}
        """
        if no_data is not None and 'float' in str(data.dtype):
            data[data == no_data] = np.nan

        if values is None:
            if "float" in str(data.dtype):
                min_value = math.ceil(np.nanquantile(data, 0.25))
                max_value = math.ceil(np.nanquantile(data, 0.75))
                values = []
                prev_val = None
                for v in range(min_value, max_value):
                    if v in [min_value, max_value]:
                        values.append((v,))
                    else:
                        values.append((prev_val, v))
                    prev_val = v
            else:
                values = np.unique(data)
                values = values[values != no_data]
        else:
            if "float" in str(data.dtype) and not isinstance(values[0], tuple):
                val = [float(v) for v in values]
                values = []
                for i in range(len(val)):
                    if i == 0 or i == len(val) - 1:
                        values.append((val[i],))
                    else:
                        values.append((val[i - 1], val[i]))

        # Helper to resolve label
        def resolve_label(val, idx):
            if value_labels is None:
                return None
            if callable(value_labels):
                return value_labels(val)
            if isinstance(value_labels, dict):
                return value_labels.get(val)
            if isinstance(value_labels, (list, tuple)):
                return value_labels[idx] if idx < len(value_labels) else None
            return None

        output = []
        for idx, v in enumerate(values):
            if isinstance(v, tuple):
                if len(v) == 1 and idx == 0:
                    count = np.count_nonzero(data <= v[0])
                    key = f"<= {v[0]}"
                elif len(v) == 1 and idx != 0:
                    count = np.count_nonzero(data >= v[0])
                    key = f">= {v[0]}"
                else:
                    count = np.count_nonzero((data > v[0]) & (data <= v[1]))
                    key = " - ".join(map(str, v))
            else:
                v = float(v) if isinstance(v, str) else v
                count = np.count_nonzero(data == v)
                key = str(v)

            item = {"value": key, "count": count}
            lbl = resolve_label(key, idx)
            if lbl is not None:
                item["label"] = lbl
            output.append(item)

        return output

    @staticmethod
    def get_boolean_raster(data: np.ndarray, pixel_value: Union[List, int]):
        """
        Converts raster data into a binary raster where target pixel values are set to 1.

        Parameters
        ----------
        data : np.ndarray
            Input raster data.
        pixel_value : int or list
            Single pixel value or list of values to match.
        """
        # Ensure pixel_value is always a list
        if not isinstance(pixel_value, (list, tuple, np.ndarray)):
            pixel_value = [pixel_value]

        con = None
        for v in pixel_value:
            con = data == v if con is None else np.logical_or(con, data == v)

        res = np.where(con, 1, 0).astype(np.uint8)
        return res

    @staticmethod
    def create_distance_raster(data: np.ndarray, pixel_value: Union[List, int], pixel_size_in_km:float=1):
        """
        Creates a distance raster where each cell contains the distance (in km) to the
        nearest target pixel defined by `pixel_value`.

        The function first converts a raster to a binary form (target = 1, others = 0)
        and then applies KDTree nearest neighbor search to compute Euclidean distance.

        Parameters
        ----------
        data : np.ndarray
            Input raster as a 2D array.
        pixel_value : list or int
            Pixel values in `data` to be treated as target locations. Distance will be
            computed to the nearest of these target pixels.
        pixel_size_in_km : float, optional
            Size of one pixel in kilometers. Default is 1 km. Used to scale the output
            distances to real-world units.

        Returns
        -------
        np.ndarray
            A 2D numpy array (float64) containing distance of each pixel to the nearest
            target pixel. Distances are in kilometers.
        """
        # Create binary raster where target pixels are 1
        boolean_data = BandProcess.get_boolean_raster(data, pixel_value)

        # Prepare distance array with same shape as input
        dist_array = np.empty(boolean_data.shape, dtype=float)

        try:
            print("Calculating distance raster...")

            # Find coordinates of target pixels (value == 1)
            target_coords = np.array(np.where(boolean_data == 1)).T
            tree = KDTree(data=target_coords, leafsize=64)

            t_start = time()

            # Iterate over each pixel in the raster
            for cur_index, val in np.ndenumerate(boolean_data):
                # Query nearest target pixel using KDTree
                min_dist, min_index = tree.query([cur_index])

                if len(min_dist) > 0:
                    # Distance is returned in pixel units, multiply by pixel size
                    dist_array[cur_index[0]][cur_index[1]] = min_dist[0] * pixel_size_in_km

            print(f"Distance calculation completed in {round(time() - t_start, 4)} seconds")

        except Exception as e:
            print(f"Error while creating distance raster: {str(e)}")

        return dist_array

    @staticmethod
    def resize_to_match(arr1: np.ndarray, arr2: np.ndarray):
        """ Resize one array to match the shape of the other using interpolation. """
        target_shape = arr1.shape if arr1.size > arr2.size else arr2.shape
        arr1_resized = cv2.resize(arr1, (target_shape[1], target_shape[0]), interpolation=cv2.INTER_LINEAR)
        arr2_resized = cv2.resize(arr2, (target_shape[1], target_shape[0]), interpolation=cv2.INTER_LINEAR)

        return arr1_resized, arr2_resized

    @staticmethod
    def find_local_extreme(
            data: np.ndarray,
            stats_value: Literal["min", "max"],
            window_size: Tuple[int, int]
    ) -> tuple[signedinteger[Any], ...] | None:
        """
        Finds the (row, col) of the min/max value.
        Returns None if the array is empty or all NaNs.
        """
        if data.size == 0 or np.isnan(data).all():
            return None

        # Convert to float to handle NaNs correctly
        work_arr = data.astype(np.float64)

        # Apply smoothing
        smoothed = cv2.blur(work_arr, window_size, borderType=cv2.BORDER_CONSTANT)

        # Final safety check after blur
        if np.isnan(smoothed).all():
            return None

        idx = np.nanargmin(smoothed) if stats_value == "min" else np.nanargmax(smoothed)
        return np.unravel_index(idx, smoothed.shape)

    @staticmethod
    def calculate_band_entropy(
            arr: np.ndarray,
            bins: int = 100,
    ) -> tuple[float, float] | tuple[float, float, str]:
        """
        Calculates Shannon Information Entropy and Relative Entropy.

        Returns
        -------
        entropy : float
            Shannon entropy (bits).

        relative_entropy : float
            Normalized entropy in the range [0,1].
        """

        def interpret_relative_entropy(relative_entropy: float) -> str:
            if relative_entropy >= 0.90:
                return "Very high information content"
            if relative_entropy >= 0.75:
                return "High information content"
            if relative_entropy >= 0.50:
                return "Moderate information content"
            if relative_entropy >= 0.25:
                return "Low information content"
            return "Very low information content"

        valid_pixels = arr[np.isfinite(arr)]

        if valid_pixels.size == 0:
            return 0.0, 0.0

        counts, _ = np.histogram(valid_pixels, bins=bins)

        probabilities = counts / counts.sum()
        probabilities = probabilities[probabilities > 0]

        entropy = -np.sum(
            probabilities * np.log2(probabilities)
        )

        max_entropy = np.log2(bins)

        relative_entropy = (
            entropy / max_entropy
            if max_entropy > 0
            else 0.0
        )
        interpretation = interpret_relative_entropy(relative_entropy)

        return float(entropy), float(relative_entropy), interpretation