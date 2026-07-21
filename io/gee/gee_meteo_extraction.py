import calendar
from pathlib import Path
from typing import Literal, Optional

import ee
import numpy as np
from geopandas.geodataframe import GeoDataFrame

from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.io.gee.gee_image_collection import GEEImageCollection, CollectionOp
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.pipeline.gee_pipeline import GEEPipeline
from digitalarzengine.processing.raster.rio_process import RioProcess
from digitalarzengine.utils.singletons import da_logger

StatsType = Literal["max", "total", "min", "std", "median"]
MeteoParamType = Literal["pr", "fldas_et", "pet", "modis_et"]


class GEEMeteoExtractor:
    """
    Engine class responsible for harvesting and processing gridded meteorological
    datasets from Google Earth Engine to drive downstream event-based runoff models.
    """

    # Static Registry defining specific dataset properties across operational pipelines
    _DATASET_REGISTRY = {
        "pr": {
            "collection": "UCSB-CHG/CHIRPS/DAILY",
            "band": "precipitation",
            "scale": 5550,
            "multiplier": 1.0,
            "prefix": "chirps_pr",
            "start_year": 1981,
            "end_year": 2026,

        },
        "fldas_et": {
            "collection": "NASA/FLDAS/NOAH01/C/GL/M/V001",
            "band": "Evap_tavg",
            "scale": 11100,  # FLDAS native grid size (~11km)
            "multiplier": 86400.0,  # Converts kg/m²/s straight to mm/day
            "prefix": "fldas_et",
            "start_year": 1982,
            "end_year": 2026
        },
        "modis_et": {
            "collection": "MODIS/061/MOD16A2",
            "band": "ET",
            "scale": 500,
            "multiplier": 0.1,  # Applies MODIS bit-depth compression offset
            "prefix": "modis_et",
            "start_year": "2021",
            "end_year": "2026"
        },
        "pet": {
            "collection": "MODIS/061/MOD16A2",
            "band": "PET",
            "scale": 500,
            "multiplier": 0.1,  # Applies MODIS bit-depth compression offset
            "prefix": "modis_pet",
            "start_year": "1981",
            "end_year": "2026"
        }
    }

    def __init__(self, output_dir: Path, aoi_gdf: GeoDataFrame, config_fp: Path = None):
        self.output_dir = Path(output_dir)
        self.aoi_gdf = aoi_gdf
        self.config_fp = config_fp
        self._gee_pipeline: Optional[GEEPipeline] = None

        # Configure output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.output_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _get_pipeline_and_region(self) -> tuple:
        """LAZY INITIALIZATION: Checks if a pipeline connection is alive."""
        if self._gee_pipeline is None:
            da_logger.info("Initializing active Google Earth Engine session connection...")
            self._gee_pipeline = GEEPipeline(self.aoi_gdf, config_fp=str(self.config_fp))
        return self._gee_pipeline, self._gee_pipeline.region

    def _get_reducer(self, stats: StatsType = "max") -> ee.Reducer:
        match stats:
            case "max":
                return ee.Reducer.max()
            case "total":
                return ee.Reducer.sum()
            case "min":
                return ee.Reducer.min()
            case "std":
                return ee.Reducer.stdDev()
            case "median":
                return ee.Reducer.median()
            case _:
                return ee.Reducer.max()

    # =========================================================================
    # CORE REFACTORED HARVESTING WORKER (Unified Logic Base)
    # =========================================================================

    @staticmethod
    def build_seasonal_date_ranges(year: int, months: list[int]) -> list[tuple[str, str]]:
        """
        Generates clean ISO string start/end date tuples for a given target completion year.
        If months contain 11 or 12 alongside winter/spring months (1, 2, 3), it correctly
        shifts the 11/12 segment to the preceding calendar year (year - 1).

        Returns:
            list[tuple[str, str]] -> [('YYYY-MM-DD', 'YYYY-MM-DD'), ...]
        """
        anchor_month = months[0]
        # Detect if this represents a true cross-year sequential winter sequence
        has_prev_year_months = any(m in [11, 12] for m in months) and any(m in [1, 2, 3] for m in months)

        date_ranges = []
        for m in months:
            # If it's a winter sequence and the month is Nov/Dec, it belongs to the previous year
            is_prev_year = has_prev_year_months and (m in [11, 12])
            target_year = year - 1 if is_prev_year else year

            last_day = calendar.monthrange(target_year, m)[1]
            start_date = f"{target_year}-{m:02d}-01"
            end_date = f"{target_year}-{m:02d}-{last_day:02d}"
            date_ranges.append((start_date, end_date))

        return date_ranges

    def _harvest_custom_series(
            self,
            param: MeteoParamType,
            stats: StatsType = "max",
            start_year: int = None,
            end_year: int = None,
            months: list[int] = None,
            custom_filepath: Path = None
    ) -> RioRaster:
        """
        Unified implementation handler matching selective month lists, filtering boundary
        geometries, reducing collections, and handling downloading mechanics.
        """
        meta = self._DATASET_REGISTRY[param]
        if months is None:
            months = list(range(1, 13))
        start_year = meta["start_year"] if start_year is None else start_year
        end_year = meta["end_year"] if end_year is None else end_year
        if not all(1 <= m <= 12 for m in months):
            raise ValueError("All elements inside months array must be integers between 1 and 12.")


        months_tag = "_".join(map(str, months))

        if custom_filepath is None:
            filename = f"{meta['prefix']}_m{months_tag}_{stats}_{start_year}_{end_year}.tif"
            custom_filepath = self.output_dir / filename

        if custom_filepath.exists():
            return RioRaster(custom_filepath)

        # Trigger lazy connection initialization
        gee_pipeline, region = self._get_pipeline_and_region()

        da_logger.info(f"Harvesting {param.upper()} Series for selective months ({months}) from GEE...")

        # CRITICAL FIX: Lock the target band footprint right here at selection initialization
        base_collection = ee.ImageCollection(meta["collection"]).select(meta["band"])
        reducer = self._get_reducer(stats)

        years = list(range(start_year, end_year + 1))
        reduced_images_list = []

        has_prev_year_months = any(m in [11, 12] for m in months) and any(m in [1, 2, 3] for m in months)

        for y in years:
            try:
                date_segments = self.build_seasonal_date_ranges(y, months)
            except Exception as e:
                da_logger.warning(f"Skipping date window compilation for year {y}: {e}")
                continue

            # NATIVE FIX: Use server-side image collection filters entirely
            # Instead of .getInfo() unpacking, we chain .filterDate via native iterative merging
            year_slices = []
            for start_date_str, end_date_str in date_segments:
                slice_collection = base_collection.filterDate(start_date_str, end_date_str).filterBounds(
                    region.get_aoi())
                year_slices.append(slice_collection)

            if not year_slices:
                continue

            # Merge the sub-collections server-side
            combined_year_collection = year_slices[0]
            for idx in range(1, len(year_slices)):
                combined_year_collection = combined_year_collection.merge(year_slices[idx])

            # Defensive Guard: Verify if imagery actually exists for this combined set
            if combined_year_collection.size().getInfo() == 0:
                da_logger.warning(
                    f"⚠️ No active data fragments found in GEE for {param.upper()} during seasonal cycle ending in year {y}. Skipping.")
                continue

            # Execute temporal reduction server-side (Guaranteed to be 1 single band now!)
            processed_layer = combined_year_collection.reduce(reducer)

            # Apply scale factor adjustments if necessary
            if meta["multiplier"] != 1.0:
                processed_layer = processed_layer.multiply(meta["multiplier"])

            # Apply chronological band descriptors (e.g., fldas_et_max_2023_2024)
            year_tag = f"{y - 1}_{y}" if has_prev_year_months else f"{y}"
            processed_layer = processed_layer.rename(f"{param}_{stats}_{year_tag}")
            reduced_images_list.append(processed_layer)

        if not reduced_images_list:
            raise RuntimeError(f"Zero valid data frames were captured for {param.upper()} across the entire timeline.")

        # Convert your final stack list back to a clean imagery band profile to download
        # ee.ImageCollection.fromImages handles lists of server-side ee.Image objects perfectly
        final_stack_collection = ee.ImageCollection.fromImages(reduced_images_list)

        res = GEEImage(final_stack_collection.toBands()).download_image(custom_filepath, region, scale=meta["scale"])

        if not res.ok:
            if custom_filepath.exists():
                custom_filepath.unlink()
            raise RuntimeError(f"Failed to harvest {param.upper()} historical stack asset from GEE.")

        return RioRaster(custom_filepath)
    # =========================================================================
    # PUBLIC CLEAN WRAPPER INTERFACES
    # =========================================================================

    def get_custom_pr_series(self, **kwargs) -> RioRaster:
        """Extracts custom seasonal precipitation multi-band stacks locally from CHIRPS."""
        return self._harvest_custom_series(param="pr", **kwargs)

    def get_custom_et_series(self, band: MeteoParamType, **kwargs) -> RioRaster:
        """Extracts custom seasonal ET/PET multi-band stacks locally from MODIS."""
        # Dynamic override based on requested keyword band layer argument string
        # param_tag = "pet" if band.upper() == "PET" else "et"
        # param_tag =  band.lower()  if band.lower() in self._DATASET_REGISTRY.keys() else "pet"
        return self._harvest_custom_series(param=band, **kwargs)

    # =========================================================================
    # SPECIALIZED CALCULATIONS / EXTREME VALUES
    # =========================================================================

    def calculate_rp_precipitation(self, annual_stack_raster: RioRaster, return_period: int,
                                   output_fp=None) -> RioRaster:
        """Runs locally using RioProcess—bypasses GEE completely if file exists."""
        if output_fp is None:
            output_fp = self.output_dir / f"pr_rp_{return_period}_gev.tif"
        if output_fp.exists():
            return RioRaster(output_fp)

        da_logger.info("Initiating pixel-by-pixel GEV distribution fitting via RioProcess...")
        RioProcess.get_return_period_surfaces(
            raster=annual_stack_raster,
            output_path=str(output_fp),
            return_periods=(return_period,)
        )
        return RioRaster(output_fp)

    def calculate_rp_evapotranspiration(self, pet_stack_raster: RioRaster, return_period: int,
                                        output_fp: Path = None) -> RioRaster:
        """
        Method 2 (PET): Hands off the cached multi-band seasonal MODIS PET stack raster
        to RioProcess to execute a pixel-by-pixel GEV distribution fit for a specific
        return period.
        """
        if not isinstance(return_period, int):
            raise TypeError(f"return_period must be an integer, received {type(return_period)}")

        if output_fp is None:
            output_fp = self.output_dir / f"pet_rp_{return_period}_gev.tif"

        if output_fp.exists():
            da_logger.info(f"Cached PET return period surface found at: {output_fp}")
            return RioRaster(output_fp)

        da_logger.info(f"Initiating pixel-by-pixel GEV fitting for a {return_period}-year PET event footprint...")

        # Call your existing static GEV processing loop engine
        RioProcess.get_return_period_surfaces(
            raster=pet_stack_raster,
            output_path=str(output_fp),
            return_periods=(return_period,)
        )

        if not output_fp.exists():
            raise RuntimeError(f"RioProcess executed but failed to save PET return period surface at {output_fp}")

        da_logger.success(f"GEV PET design storm surface successfully saved to {output_fp}")
        return RioRaster(output_fp)

    def reduce_historical_stack(
            self,
            stack_raster: RioRaster,
            operation: Literal["mean", "max", "min", "median", "std", "cv", "reliability"],
            output_fp: str = None
    ) -> RioRaster:
        """
        Collapses a multi-band historical raster stack (years) along the temporal axis
        into a single-band baseline surface using localized NumPy matrix operations.
        Supports advanced composite hydrological metrics including CV and Reliability.
        """
        if output_fp is None:
            base_name = Path(stack_raster.get_file_name()).stem
            output_fp = self.output_dir / f"{base_name}_reduced_{operation}.tif"

        if output_fp.exists():
            return RioRaster(output_fp)

        da_logger.info(f"Collapsing historical raster stack layers via matrix '{operation}' operation...")

        # 1. Extract 3D NumPy array matrix. Layout Shape: (bands/years, rows, cols)
        stack_matrix = stack_raster.get_data_array()
        nodata_val = stack_raster.get_nodata_value()

        # 2. Mask out NoData value artifacts to preserve mathematical integrity
        masked_matrix = np.where(stack_matrix == nodata_val, np.nan, stack_matrix)

        # 3. Process calculations cleanly using unified branch routing
        if operation in ["mean", "max", "min", "median", "std"]:
            match operation:
                case "mean":
                    reduced_data = np.nanmean(masked_matrix, axis=0)
                case "max":
                    reduced_data = np.nanmax(masked_matrix, axis=0)
                case "min":
                    reduced_data = np.nanmin(masked_matrix, axis=0)
                case "median":
                    reduced_data = np.nanmedian(masked_matrix, axis=0)
                case "std":
                    reduced_data = np.nanstd(masked_matrix, axis=0)

        elif operation in ["cv", "reliability"]:
            mean_array = np.nanmean(masked_matrix, axis=0)
            std_array = np.nanstd(masked_matrix, axis=0)

            # Map valid calculation areas to avoid zero division over arid pixels
            safe_mean_mask = (mean_array > 0) & (~np.isnan(mean_array))

            # FIX: Eliminated the hazardous 'np.full_file' call syntax layout
            cv_array = np.full(mean_array.shape, np.nan, dtype=np.float32)
            cv_array[safe_mean_mask] = std_array[safe_mean_mask] / mean_array[safe_mean_mask]

            if operation == "cv":
                reduced_data = cv_array
            elif operation == "reliability":
                # Reliability = 1 - CV (Values closer to 1 signify stable environments)
                reliability_array = 1.0 - cv_array
                reduced_data = np.maximum(reliability_array, 0)
        else:
            raise ValueError(f"Unsupported reduction operation: {operation}")

        # Step 4. Re-inject the original NoData value back into empty background spaces
        final_band_data = np.where(np.isnan(reduced_data), nodata_val, reduced_data)

        # =========================================================================
        # DEFENSIVE DATA TYPE CASTING
        # =========================================================================
        # FIX: Explicitly cast away from generic python 'object' to a unified
        # float32 primitive that rasterio and GDAL understand perfectly.
        final_band_data = final_band_data.astype(np.float32)

        # 1. Expand the 2D array (rows, cols) to 3D (1, rows, cols) to match rasterio specs
        if final_band_data.ndim == 2:
            final_band_data = final_band_data[np.newaxis, ...]

        # 2. Create in-memory RioRaster instance safely
        final_raster = RioRaster.raster_from_array(
            img_arr=final_band_data,
            crs=stack_raster.get_crs(),
            g_transform=stack_raster.get_geo_transform(),
            nodata_value=float(nodata_val) if nodata_val is not None else None
        )

        # 3. Commit the file to disk with custom descriptions
        final_raster.save_to_file(
            img_des=output_fp,
            band_names=[f"Historical Temporal {operation.upper()}"]
        )

        da_logger.success(f"Successfully saved collapsed single-band baseline map to {output_fp}")
        return RioRaster(output_fp)

if __name__ == "__main__":
    OUTPUT_DIR = Path("output")
    aoi_gdf = GeoDataFrame()
    config_path = Path("config.yaml")
    # 1. Initialize the extractor service
    extractor = GEEMeteoExtractor(output_dir=OUTPUT_DIR, aoi_gdf=aoi_gdf, config_fp=config_path)

    # 2. HARVEST STACKS FROM GEE (Only runs once, downloads cached to disk)
    # Pull the annual maximum storm rainfall series (CHIRPS)
    pr_stack = extractor.get_custom_pr_series(stats="max", start_month=1, end_month=12)

    # Pull the annual maximum potential evapotranspiration series (MODIS)
    # Using 'max' here tracks the maximum potential water loss spikes over the years
    pet_stack = extractor.get_custom_et_series(band="PET", stats="max", start_month=1, end_month=12)

    # 3. COMPUTE LOCAL GEV RETURN PERIODS INSTANTANEOUSLY
    target_return_periods = [10, 25, 50, 100]

    for rp in target_return_periods:
        da_logger.info(f"Processing meteorological boundary surfaces for a {rp}-year event...")

        # Generate standalone design storm precipitation surface
        pr_rp_raster = extractor.calculate_rp_precipitation(pr_stack, return_period=rp)

        # Generate standalone design atmospheric demand PET surface
        pet_rp_raster = extractor.calculate_rp_evapotranspiration(pet_stack, return_period=rp)


        # 4. ROUTE DIRECTLY INTO YOUR BUCKET LOSS MODEL
        # loss_results = build_loss_model(
        #     rainfall_raster=pr_rp_raster,
        #     actual_et_raster=pet_rp_raster,
        #     soil_depth_cm=30
        # )
    historical_stack = extractor.reduce_historical_stack(pr_stack, operation="mean")