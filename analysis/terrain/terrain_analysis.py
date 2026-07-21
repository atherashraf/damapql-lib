import math
import os
import enum
from dataclasses import dataclass
from typing import Tuple, Optional, Union, Callable, Dict, Any

import ee
import numpy as np
import rasterio
from whitebox import WhiteboxTools

from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.io.rio_raster import RioRaster


# -----------------------------
# Product registry
# -----------------------------
@dataclass(frozen=True)
class ProductMeta:
    name: str
    units: Optional[str]
    description: str


class Products(enum.Enum):
    # Base
    DEM = "dem"
    FLOW_DIR = "flow_dir"

    # Intermediate
    DEM_FILLED = "dem_filled"

    # Derivatives
    FLOW_ACC = "flow_acc"     # cells
    SLOPE_DEG = "slope_deg"
    SLOPE_RAD = "slope_rad"
    SLOPE_PCT = "slope_pct"
    ASPECT = "aspect"
    PLAN_CURV = "plan_curvature"
    PROFILE_CURV = "profile_curvature"
    RELIEF = "relative_relief"
    VALLEY_DEPTH = "valley_depth"

    # Indices
    TRI = "tri"
    SCA = "sca"               # specific contributing area (m²/m)
    TWI = "twi"
    SPI = "spi"
    TPI = "tpi"
    CONV_INDEX = "convergence_index"

    @staticmethod
    def get_product_by_values(value: str) -> "Products | None":
        for product in Products:
            if product.value == value:
                return product
        return None




PRODUCT_META = {
    Products.DEM: ProductMeta(
        name="Digital Elevation Model",
        units="m",
        description="Bare-earth elevation above mean sea level"
    ),
    Products.DEM_FILLED: ProductMeta(
        name="Filled DEM",
        units="m",
        description="DEM after sink filling (hydrologically corrected)"
    ),
    Products.FLOW_DIR: ProductMeta(
        name="Flow Direction (D8)",
        units="code",
        description=(
            "D8 local drainage direction codes (MERIT Hydro dir band): "
            "1=E, 2=SE, 4=S, 8=SW, 16=W, 32=NW, 64=N, 128=NE; "
            "0=river mouth; -1=inland depression"
        )
    ),
    Products.FLOW_ACC: ProductMeta(
        name="Flow Accumulation",
        units="cells",
        description="D8 flow accumulation as number of upslope contributing cells"
    ),
    Products.SLOPE_DEG: ProductMeta(
        name="Slope",
        units="degrees",
        description="Slope angle in degrees"
    ),
    Products.SLOPE_RAD: ProductMeta(
        name="Slope",
        units="radians",
        description="Slope angle in radians (used in TWI/SPI)"
    ),
    Products.SLOPE_PCT: ProductMeta(
        name="Slope",
        units="percent",
        description="Slope expressed as rise over run (%)"
    ),
    Products.ASPECT: ProductMeta(
        name="Aspect",
        units="degrees",
        description="Downslope direction clockwise from North (0–360)"
    ),
    Products.PLAN_CURV: ProductMeta(
        name="Plan Curvature",
        units="1/m",
        description="Flow convergence/divergence curvature"
    ),
    Products.PROFILE_CURV: ProductMeta(
        name="Profile Curvature",
        units="1/m",
        description="Flow acceleration/deceleration curvature"
    ),
    Products.SCA: ProductMeta(
        name="Specific Contributing Area (SCA)",
        units="m²/m",
        description="D8 specific contributing area used in TWI/SPI"
    ),
    Products.TWI: ProductMeta(
        name="Topographic Wetness Index (TWI)",
        units="ln(m²/m)",
        description="ln(SCA / tan(slope)); relative wetness / accumulation potential"
    ),
    Products.SPI: ProductMeta(
        name="Stream Power Index (SPI)",
        units="(m²/m)",
        description="SCA * tan(slope); proxy for flow energy / erosive power"
    ),
    Products.TRI: ProductMeta(
        name="Terrain Ruggedness Index (TRI)",
        units="m",
        description="Ruggedness based on elevation differences with neighbors"
    ),
    Products.RELIEF: ProductMeta(
        name="Relative Relief",
        units="m",
        description="Local relief = moving-window (max DEM - min DEM)"
    ),

    Products.VALLEY_DEPTH: ProductMeta(
        name="Valley Depth",
        units="m",
        description=(
            "Vertical distance to a modeled valley bottom (relative terrain position). "
            "Higher values generally indicate higher positions above valley floors."
        ),
    ),

    Products.TPI: ProductMeta(
        name="Topographic Position Index (TPI)",
        units="m",
        description="TPI = elevation - mean elevation of neighborhood window (negative=valley, positive=ridge)."
    ),

    Products.CONV_INDEX: ProductMeta(
        name="Convergence Index",
        units="degrees",
        description=(
            "Terrain convergence/divergence index. Positive values often indicate "
            "convergent areas (valley-like), negative values divergent areas (ridge-like)."
        ),
    ),
}


# -----------------------------
# TerrainAnalysis
# -----------------------------
class TerrainAnalysis:
    """
    Design:
      - One instance = one consistent scale (resolution)
      - Raw inputs in raw/
      - Intermediate in temp/
      - Terrain derivatives in derivatives/
      - Indices in indices/
    """

    def __init__(self, data_dir: str, scale: int = 90, gee_pipeline=None):
        self.data_dir = data_dir
        self.scale = int(scale)
        self.gee_pipeline = gee_pipeline

        # canonical subfolders (scale-aware)
        self.raw_dir = os.path.join(data_dir, f"scale_{self.scale}")
        self.idx_dir = os.path.join(self.raw_dir, "indices")
        self.derivatives_dir = os.path.join(self.raw_dir, "derivatives")
        self.temp_dir = os.path.join(self.raw_dir, "temp")

        os.makedirs(self.raw_dir, exist_ok=True)
        os.makedirs(self.idx_dir, exist_ok=True)
        os.makedirs(self.derivatives_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

        self.wbt = WhiteboxTools()
        self.wbt.work_dir = self.temp_dir

        self._eps = 1e-6

        # ✅ Validate after paths exist
        if self.gee_pipeline is None and self.is_gee_pipeline_required():
            raise ValueError(
                "gee_pipeline is required because DEM and/or FLOW_DIR are missing.\n"
                f"Expected DEM at: {self.get_product_file_path(Products.DEM)}\n"
                f"Expected FLOW_DIR at: {self.get_product_file_path(Products.FLOW_DIR)}"
            )

    # -----------------------------
    # Product metadata helpers
    # -----------------------------
    @staticmethod
    def get_product_name(product: Products) -> str:
        return PRODUCT_META[product].name

    @staticmethod
    def get_product_units(product: Products) -> Optional[str]:
        return PRODUCT_META[product].units

    @staticmethod
    def get_product_description(product: Products) -> str:
        return PRODUCT_META[product].description

    # -----------------------------
    # Product → file path (single source of truth)
    # -----------------------------

    def is_gee_pipeline_required(self) -> bool:
        """
        Returns True if GEE is required to proceed, i.e.
        DEM or FLOW_DIR does not exist on disk.

        This method does NOT touch Earth Engine.
        """
        dem_fp = self.get_product_file_path(Products.DEM)
        flow_dir_fp = self.get_product_file_path(Products.FLOW_DIR)

        dem_exists = os.path.exists(dem_fp)
        flow_dir_exists = os.path.exists(flow_dir_fp)

        return not (dem_exists and flow_dir_exists)


    def get_product_file_path(self, product: Products) -> str:
        product_map = {
            # Base
            Products.DEM: os.path.join(self.raw_dir, "merit_dem.tif"),
            Products.FLOW_DIR: os.path.join(self.raw_dir, "merit_flow_dir.tif"),

            # Intermediate
            Products.DEM_FILLED: os.path.join(self.temp_dir, "dem_filled.tif"),

            # Derivatives
            Products.FLOW_ACC: os.path.join(self.derivatives_dir, "flow_acc_cells.tif"),
            Products.SLOPE_DEG: os.path.join(self.derivatives_dir, "slope_deg.tif"),
            Products.SLOPE_RAD: os.path.join(self.derivatives_dir, "slope_rad.tif"),
            Products.SLOPE_PCT: os.path.join(self.derivatives_dir, "slope_pct.tif"),
            Products.ASPECT: os.path.join(self.derivatives_dir, "aspect.tif"),
            Products.PLAN_CURV: os.path.join(self.derivatives_dir, "plan_curvature.tif"),
            Products.PROFILE_CURV: os.path.join(self.derivatives_dir, "profile_curvature.tif"),
            Products.RELIEF: os.path.join(self.idx_dir, "relative_relief_m.tif"),
            Products.VALLEY_DEPTH: os.path.join(self.idx_dir, "valley_depth.tif"),

            # Indices
            Products.SCA: os.path.join(self.idx_dir, "sca_m2_per_m.tif"),
            Products.TWI: os.path.join(self.idx_dir, "twi.tif"),
            Products.SPI: os.path.join(self.idx_dir, "spi.tif"),
            Products.TRI: os.path.join(self.idx_dir, "tri.tif"),
            Products.TPI: os.path.join(self.idx_dir, "tpi.tif"),
            Products.CONV_INDEX: os.path.join(self.idx_dir, "convergence_index.tif"),

        }
        try:
            return product_map[product]
        except KeyError:
            raise ValueError(f"Unsupported product: {product}")

    # -----------------------------
    # Raster prep utilities
    # -----------------------------
    def _ensure_raster_scale(self, fp: str, resampling: str = "nearest") -> str:
        """
        Ensure raster is approximately at self.scale. Resamples in-place using RioRaster if needed.
        """
        raster = RioRaster(fp)
        x_res, y_res = raster.get_spatial_resolution()
        if not (math.isclose(x_res, self.scale, rel_tol=0.1) and math.isclose(y_res, self.scale, rel_tol=0.1)):
            raster.resample(self.scale, resampling=resampling)
        return fp

    def sanitize_for_whitebox(
        self,
        src_path: str,
        dst_path: Optional[str] = None,
        compress: str = "DEFLATE",
        tiled: bool = True,
        nodata_value: float = -9999.0,
    ) -> str:
        """
        Rewrites a GeoTIFF so WhiteboxTools can read it reliably:
          - dtype float32
          - nodata numeric (default -9999)
          - predictor=1 (avoid float predictor issues)
          - optional DEFLATE + tiling
        """
        if dst_path is None:
            base, ext = os.path.splitext(src_path)
            dst_path = f"{base}_wb.tif"
        # if dst_path is None:
        #     # keep sanitized files inside temp/
        #     base = os.path.splitext(os.path.basename(src_path))[0]
        #     dst_path = os.path.join(self.temp_dir, f"{base}_wb.tif")

        # Cache: if sanitized file exists and is newer than source, reuse it
        if os.path.exists(dst_path):
            try:
                if os.path.getmtime(dst_path) >= os.path.getmtime(src_path):
                    return dst_path
            except OSError:
                pass

        with rasterio.open(src_path) as src:
            data = src.read(1)
            profile = src.profile.copy()

        data = data.astype("float32", copy=False)
        data = np.where(np.isfinite(data), data, nodata_value)

        profile.update(
            dtype="float32",
            nodata=float(nodata_value),
            BIGTIFF="IF_SAFER",
        )

        if compress:
            profile.update(compress=compress, predictor=1)
        else:
            profile.update(compress=None, predictor=1)

        if tiled:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        else:
            profile.update(tiled=False)
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)

        with rasterio.open(dst_path, "w", **profile) as dst:
            dst.write(data, 1)

        return dst_path

    # -----------------------------
    # Download / ensure base products
    # -----------------------------
    def _ensure_dem_exists(self) -> str:
        dem_fp = self.get_product_file_path(Products.DEM)
        if not os.path.exists(dem_fp):
            if self.gee_pipeline is None:
                raise ValueError(f"gee_pipeline must be provided to download DEM at {dem_fp}")
            region = self.gee_pipeline.region
            img = ee.Image("MERIT/DEM/v1_0_3").select("dem")
            GEEImage(img).download_image(
                dem_fp, region, self.scale, within_aoi_only=False, save_metadata=False
            )
        self._ensure_raster_scale(dem_fp, resampling="nearest")
        return dem_fp

    def _ensure_flow_dir_exists(self) -> str:
        fp = self.get_product_file_path(Products.FLOW_DIR)
        if not os.path.exists(fp):
            if self.gee_pipeline is None:
                raise ValueError(f"gee_pipeline must be provided to download flow dir at {fp}")
            region = self.gee_pipeline.region
            img = ee.Image("MERIT/Hydro/v1_0_1").select("dir")
            GEEImage(img).download_image(fp, region, self.scale, within_aoi_only=False, save_metadata=False)
        self._ensure_raster_scale(fp, resampling="nearest")
        return fp

    # -----------------------------
    # Intermediate: filled DEM
    # -----------------------------
    def _ensure_filled_dem(self) -> str:
        """
        Ensures:
          - DEM exists (download if needed)
          - DEM is sanitized for Whitebox
          - sinks filled (Wang & Liu) -> DEM_FILLED
        Returns path to filled DEM.
        """
        dem_fp = self._ensure_dem_exists()
        dem_wb_fp = self.sanitize_for_whitebox(dem_fp)

        dem_filled_fp = self.get_product_file_path(Products.DEM_FILLED)
        if not os.path.exists(dem_filled_fp):
            self.wbt.fill_depressions_wang_and_liu(dem=dem_wb_fp, output=dem_filled_fp)

        return dem_filled_fp

    # -----------------------------
    # Helpers: SCA + slope(rad)
    # -----------------------------
    def _ensure_sca_and_slope_rad(self) -> Tuple[str, str]:
        dem_filled_fp = self._ensure_filled_dem()

        sca_fp = self.get_product_file_path(Products.SCA)
        if not os.path.exists(sca_fp):
            self.wbt.d8_flow_accumulation(
                i=dem_filled_fp,
                output=sca_fp,
                out_type="Specific Contributing Area"
            )

        slope_rad_fp = self.get_product_file_path(Products.SLOPE_RAD)
        if not os.path.exists(slope_rad_fp):
            self.wbt.slope(dem=dem_filled_fp, output=slope_rad_fp, zfactor=1.0, units="radians")

        return sca_fp, slope_rad_fp

    # -----------------------------
    # Public: Base getters
    # -----------------------------
    def get_dem(self) -> str:
        return self._ensure_dem_exists()

    def get_flow_dir(self) -> str:
        return self._ensure_flow_dir_exists()

    # -----------------------------
    # Public: Derivatives
    # -----------------------------
    def get_flow_accumulation(self, kind: str = "cells") -> str:
        """
        kind:
          - "cells" -> Products.FLOW_ACC (D8 contributing cells)
          - "sca"   -> Products.SCA      (specific contributing area, m²/m)
        """
        kind = kind.lower().strip()
        if kind not in ("cells", "sca"):
            raise ValueError("kind must be 'cells' or 'sca'")

        dem_filled_fp = self._ensure_filled_dem()

        if kind == "cells":
            out_fp = self.get_product_file_path(Products.FLOW_ACC)
            out_type = "Cells"
        else:
            out_fp = self.get_product_file_path(Products.SCA)
            out_type = "Specific Contributing Area"

        if os.path.exists(out_fp):
            return out_fp

        self.wbt.d8_flow_accumulation(i=dem_filled_fp, output=out_fp, out_type=out_type)
        return out_fp

    def get_slope(self, units: str = "degrees") -> str:
        """
        units: "degrees" | "radians" | "percent"
        """
        units = units.lower().strip()
        if units not in ("degrees", "radians", "percent"):
            raise ValueError("units must be 'degrees', 'radians', or 'percent'")

        product_map = {
            "degrees": Products.SLOPE_DEG,
            "radians": Products.SLOPE_RAD,
            "percent": Products.SLOPE_PCT,
        }
        slope_fp = self.get_product_file_path(product_map[units])
        if os.path.exists(slope_fp):
            return slope_fp

        dem_filled_fp = self._ensure_filled_dem()

        if units in ("degrees", "radians"):
            self.wbt.slope(dem=dem_filled_fp, output=slope_fp, zfactor=1.0, units=units)
            return slope_fp

        # percent slope from radians
        slope_rad_fp = self.get_product_file_path(Products.SLOPE_RAD)
        if not os.path.exists(slope_rad_fp):
            self.wbt.slope(dem=dem_filled_fp, output=slope_rad_fp, zfactor=1.0, units="radians")

        with rasterio.open(slope_rad_fp) as src:
            slope_rad = src.read(1).astype("float64")
            profile = src.profile.copy()

        slope_pct = np.tan(slope_rad) * 100.0
        nodata = profile.get("nodata", None)
        if nodata is not None:
            slope_pct[slope_rad == nodata] = nodata

        profile.update(dtype="float32")
        with rasterio.open(slope_fp, "w", **profile) as dst:
            dst.write(slope_pct.astype("float32"), 1)

        return slope_fp

    def get_aspect(self) -> str:
        aspect_fp = self.get_product_file_path(Products.ASPECT)
        if os.path.exists(aspect_fp):
            return aspect_fp

        dem_filled_fp = self._ensure_filled_dem()
        self.wbt.aspect(dem=dem_filled_fp, output=aspect_fp, zfactor=1.0)
        return aspect_fp

    def get_curvature(self) -> Tuple[str, str]:
        """
        Returns (profile_curvature_fp, plan_curvature_fp)
        """
        prof_fp = self.get_product_file_path(Products.PROFILE_CURV)
        plan_fp = self.get_product_file_path(Products.PLAN_CURV)

        if os.path.exists(prof_fp) and os.path.exists(plan_fp):
            return prof_fp, plan_fp

        dem_filled_fp = self._ensure_filled_dem()

        if not os.path.exists(prof_fp):
            self.wbt.profile_curvature(dem=dem_filled_fp, output=prof_fp, zfactor=1.0)
        if not os.path.exists(plan_fp):
            self.wbt.plan_curvature(dem=dem_filled_fp, output=plan_fp, zfactor=1.0)

        return prof_fp, plan_fp

    def get_relative_relief(self, window: int = 9) -> str:
        """
        Relative Relief (local relief) = moving-window (max elevation - min elevation).

        Parameters
        ----------
        window : int
            Odd window size in pixels (e.g., 3,5,7,9...). Default 9.

        Returns
        -------
        str
            Path to relative relief raster (meters).
        """
        if window < 3 or window % 2 == 0:
            raise ValueError("window must be an odd integer >= 3")

        out_fp = self.get_product_file_path(Products.RELIEF)
        if os.path.exists(out_fp):
            return out_fp

        # Use filled DEM (hydrologically consistent surface)
        dem_filled_fp = self._ensure_filled_dem()

        with rasterio.open(dem_filled_fp) as src:
            dem = src.read(1).astype("float32")
            profile = src.profile.copy()
            nodata = profile.get("nodata", None)

        # Build valid mask
        if nodata is None:
            valid = np.isfinite(dem)
        else:
            valid = np.isfinite(dem) & (dem != nodata)

        # Replace invalid with NaN for safe min/max ops
        dem_nan = dem.copy()
        dem_nan[~valid] = np.nan

        pad = window // 2
        padded = np.pad(dem_nan, pad_width=pad, mode="edge")

        # Sliding window min/max using numpy stride tricks
        # Result shape: (rows, cols, window, window)
        rows, cols = dem.shape
        s0, s1 = padded.strides
        shape = (rows, cols, window, window)
        strides = (s0, s1, s0, s1)
        win = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)

        local_min = np.nanmin(win, axis=(2, 3))
        local_max = np.nanmax(win, axis=(2, 3))
        relief = local_max - local_min

        # Output nodata handling
        out_nodata = -9999.0
        relief_out = np.where(np.isfinite(relief), relief, out_nodata).astype("float32")

        profile.update(dtype="float32", nodata=out_nodata)
        with rasterio.open(out_fp, "w", **profile) as dst:
            dst.write(relief_out, 1)

        return out_fp

    def get_valley_depth(self) -> str:
        """
        Compute Valley Depth from a hydrologically corrected DEM.

        Uses WhiteboxTools. Some Python builds don't expose valley_depth() as a method,
        so we fallback to run_tool("ValleyDepth", ...).
        """
        out_fp = self.get_product_file_path(Products.VALLEY_DEPTH)
        if os.path.exists(out_fp):
            return out_fp

        dem_filled_fp = self._ensure_filled_dem()

        # 1) If method exists in this build, use it
        if hasattr(self.wbt, "valley_depth"):
            try:
                self.wbt.valley_depth(dem=dem_filled_fp, output=out_fp)
                return out_fp
            except TypeError:
                # some wrappers use i= instead of dem=
                self.wbt.valley_depth(i=dem_filled_fp, output=out_fp)
                return out_fp

        # 2) Fallback: call the underlying Whitebox tool by name
        # Whitebox tool name is usually CamelCase.
        # Typical args are --dem and --output
        try:
            self.wbt.run_tool(
                "ValleyDepth",
                [f"--dem={dem_filled_fp}", f"--output={out_fp}"]
            )
            return out_fp
        except Exception:
            # Some versions use --input instead of --dem
            self.wbt.run_tool(
                "ValleyDepth",
                [f"--input={dem_filled_fp}", f"--output={out_fp}"]
            )
            return out_fp

    # -----------------------------
    # Public: Indices
    # -----------------------------
    # def get_tri(self) -> str:
    #     tri_fp = self.get_product_file_path(Products.TRI)
    #     if os.path.exists(tri_fp):
    #         return tri_fp
    #
    #     dem_filled_fp = self._ensure_filled_dem()
    #     self.wbt.terrain_ruggedness_index(dem=dem_filled_fp, output=tri_fp)
    #     return tri_fp

    def get_tri(self) -> str:
        """
        Compute Terrain Ruggedness Index (TRI).

        Uses WhiteboxTools. Method name varies by version, so we try
        known alternatives in order of preference.
        """
        tri_fp = self.get_product_file_path(Products.TRI)
        if os.path.exists(tri_fp):
            return tri_fp

        dem_filled_fp = self._ensure_filled_dem()

        # --- Try known Whitebox variants ---
        try:
            # Newer / common in Python wheels
            self.wbt.ruggedness_index(dem=dem_filled_fp, output=tri_fp)
            return tri_fp
        except AttributeError:
            pass

        try:
            # Older / documented name
            self.wbt.terrain_ruggedness_index(dem=dem_filled_fp, output=tri_fp)
            return tri_fp
        except AttributeError:
            pass

        try:
            # Fallback proxy (very common & acceptable TRI proxy)
            self.wbt.standard_deviation_of_elevation(
                dem=dem_filled_fp,
                output=tri_fp,
                filter=3
            )
            return tri_fp
        except AttributeError:
            pass

        raise RuntimeError(
            "No TRI method available in this WhiteboxTools build. "
            "Tried: ruggedness_index, terrain_ruggedness_index, "
            "standard_deviation_of_elevation."
        )

    def get_twi(self) -> str:
        twi_fp = self.get_product_file_path(Products.TWI)
        if os.path.exists(twi_fp):
            return twi_fp

        sca_fp, slope_rad_fp = self._ensure_sca_and_slope_rad()

        with rasterio.open(sca_fp) as rs, rasterio.open(slope_rad_fp) as sl:
            As = rs.read(1).astype("float64")
            beta = sl.read(1).astype("float64")
            prof = rs.profile.copy()

        As = np.where(np.isfinite(As) & (As > 0), As, np.nan)
        tanb = np.tan(beta)
        tanb = np.where(np.isfinite(tanb) & (tanb >= 0), tanb, 0)

        twi = np.log(As / (tanb + self._eps))

        # robust clip (optional but helpful)
        lo, hi = np.nanpercentile(twi, 1), np.nanpercentile(twi, 99)
        twi = np.clip(twi, lo, hi)

        out_nodata = -9999.0
        twi_out = np.where(np.isfinite(twi), twi, out_nodata).astype("float32")
        prof.update(dtype="float32", nodata=out_nodata)

        with rasterio.open(twi_fp, "w", **prof) as dst:
            dst.write(twi_out, 1)

        return twi_fp

    def get_spi(self) -> str:
        spi_fp = self.get_product_file_path(Products.SPI)
        if os.path.exists(spi_fp):
            return spi_fp

        sca_fp, slope_rad_fp = self._ensure_sca_and_slope_rad()

        with rasterio.open(sca_fp) as rs, rasterio.open(slope_rad_fp) as sl:
            As = rs.read(1).astype("float64")
            beta = sl.read(1).astype("float64")
            prof = rs.profile.copy()

        As = np.where(np.isfinite(As) & (As > 0), As, np.nan)
        tanb = np.tan(beta)
        tanb = np.where(np.isfinite(tanb) & (tanb >= 0), tanb, 0)

        spi = As * tanb

        # robust clip (optional)
        lo, hi = np.nanpercentile(spi, 1), np.nanpercentile(spi, 99)
        spi = np.clip(spi, lo, hi)

        out_nodata = -9999.0
        spi_out = np.where(np.isfinite(spi), spi, out_nodata).astype("float32")
        prof.update(dtype="float32", nodata=out_nodata)

        with rasterio.open(spi_fp, "w", **prof) as dst:
            dst.write(spi_out, 1)

        return spi_fp

    def get_tpi(self, window: int = 9, exclude_center: bool = True) -> str:
        """
        Compute Topographic Position Index (TPI).

        TPI = z(i,j) - mean(z in neighborhood)

        Parameters
        ----------
        window : int
            Odd window size in pixels (e.g., 3,5,7,9...). Default 9.
        exclude_center : bool
            If True, neighborhood mean excludes the center pixel (common choice).

        Returns
        -------
        str
            Path to TPI raster (meters).
        """
        if window < 3 or window % 2 == 0:
            raise ValueError("window must be an odd integer >= 3")

        out_fp = self.get_product_file_path(Products.TPI)
        if os.path.exists(out_fp):
            return out_fp

        dem_filled_fp = self._ensure_filled_dem()

        with rasterio.open(dem_filled_fp) as src:
            dem = src.read(1).astype("float32")
            profile = src.profile.copy()
            nodata = profile.get("nodata", None)

        # valid mask
        if nodata is None:
            valid = np.isfinite(dem)
        else:
            valid = np.isfinite(dem) & (dem != nodata)

        dem_nan = dem.copy()
        dem_nan[~valid] = np.nan

        pad = window // 2
        padded = np.pad(dem_nan, pad_width=pad, mode="edge")

        rows, cols = dem.shape
        s0, s1 = padded.strides
        shape = (rows, cols, window, window)
        strides = (s0, s1, s0, s1)
        win = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)

        # neighborhood mean
        if exclude_center:
            win2 = win.reshape(rows, cols, window * window)
            center_idx = (window * window) // 2
            neigh = np.delete(win2, center_idx, axis=2)
            mean_neigh = np.nanmean(neigh, axis=2)
        else:
            mean_neigh = np.nanmean(win, axis=(2, 3))

        tpi = dem_nan - mean_neigh

        out_nodata = -9999.0
        tpi_out = np.where(np.isfinite(tpi), tpi, out_nodata).astype("float32")

        profile.update(dtype="float32", nodata=out_nodata)
        with rasterio.open(out_fp, "w", **profile) as dst:
            dst.write(tpi_out, 1)

        return out_fp

    def get_convergence_index(self) -> str:
        """
        Compute Convergence Index from the filled DEM using WhiteboxTools.

        Many WhiteboxTools Python builds do NOT expose convergence_index() as a method,
        so we fallback to run_tool("ConvergenceIndex", ...).
        """
        out_fp = self.get_product_file_path(Products.CONV_INDEX)
        if os.path.exists(out_fp):
            return out_fp

        dem_filled_fp = self._ensure_filled_dem()

        # 1) If method exists in this build, use it
        if hasattr(self.wbt, "convergence_index"):
            try:
                self.wbt.convergence_index(dem=dem_filled_fp, output=out_fp)
                return out_fp
            except TypeError:
                self.wbt.convergence_index(i=dem_filled_fp, output=out_fp)
                return out_fp

        # 2) Fallback: call the underlying Whitebox tool by name
        # Typical args are --dem and --output
        try:
            self.wbt.run_tool(
                "ConvergenceIndex",
                [f"--dem={dem_filled_fp}", f"--output={out_fp}"]
            )
            return out_fp
        except Exception:
            # Some versions use --input instead of --dem
            self.wbt.run_tool(
                "ConvergenceIndex",
                [f"--input={dem_filled_fp}", f"--output={out_fp}"]
            )
            return out_fp



    def generate(self, product: Products, **kwargs) -> Union[str, Tuple[str, str]]:
        """
        Generate (ensure exists) a given product and return its file path.

        Examples
        --------
        ta.generate(Products.DEM)
        ta.generate(Products.SLOPE_DEG)
        ta.generate(Products.FLOW_ACC)              # cells by default
        ta.generate(Products.SCA)                   # specific contributing area
        ta.generate(Products.TPI, window=11)
        ta.generate(Products.RELIEF, window=9)
        ta.generate(Products.PROFILE_CURV)          # returns profile curvature path
        ta.generate(Products.PLAN_CURV)             # returns plan curvature path
        ta.generate(Products.VALLEY_DEPTH)
        ta.generate(Products.CONV_INDEX)
        """

        dispatch: Dict[Products, Callable[[], Any]] = {
            # Base
            Products.DEM: self.get_dem,
            Products.FLOW_DIR: self.get_flow_dir,

            # Intermediate
            Products.DEM_FILLED: self._ensure_filled_dem,

            # Derivatives
            Products.FLOW_ACC: lambda: self.get_flow_accumulation(kind="cells"),
            Products.SLOPE_DEG: lambda: self.get_slope(units="degrees"),
            Products.SLOPE_RAD: lambda: self.get_slope(units="radians"),
            Products.SLOPE_PCT: lambda: self.get_slope(units="percent"),
            Products.ASPECT: self.get_aspect,

            # curvature returns (profile, plan)
            Products.PROFILE_CURV: lambda: self.get_curvature()[0],
            Products.PLAN_CURV: lambda: self.get_curvature()[1],

            # Indices / extras
            Products.SCA: lambda: self.get_flow_accumulation(kind="sca"),
            Products.TWI: self.get_twi,
            Products.SPI: self.get_spi,
            Products.TRI: self.get_tri,
            Products.RELIEF: lambda: self.get_relative_relief(window=int(kwargs.get("window", 9))),
            Products.VALLEY_DEPTH: self.get_valley_depth,
            Products.TPI: lambda: self.get_tpi(
                window=int(kwargs.get("window", 9)),
                exclude_center=bool(kwargs.get("exclude_center", True)),
            ),
            Products.CONV_INDEX: self.get_convergence_index,
        }

        try:
            fn = dispatch[product]
        except KeyError:
            raise ValueError(f"No generator registered for product: {product}")

        return fn()


if __name__ == "__main__":
    """
    Example usage / smoke-test.

    This version works in TWO modes:
    Parameters are 
        Elevation
        Slope
        Aspect
        Curvature
            Profile curvature
            Plan curvature
        Flow Accumulation
        Flow Direction
        Topographic Wetness Index (TWI)
        Stream Power Index (SPI)
        Terrain Ruggedness Index (TRI)
        Relative Relief
        Valley Depth
        Convergence Index
        Topographic Position Index (TPI)
        
        Height Above Nearest Drainage (HAND) ----- This is missing...need  streams / drainage definition

    1) OFFLINE mode (no GEE):
       - Set use_gee = False
       - Put a DEM at:  tests/data/terrain_example/raw/merit_dem.tif
       - Then all derivatives/indices will run from that DEM.

    2) GEE mode (download DEM/flow-dir if missing):
       - Set use_gee = True
       - Provide AOI rectangle + make a tiny gee_pipeline with .region
       - Ensure ee is authenticated/initialized
    """

    # -------------------------
    # Config
    # -------------------------
    use_gee = False          # <-- set True if you want auto-download from GEE
    scale = 90
    data_dir = "tests/data/terrain_example"

    # Example AOI (only used when use_gee=True)
    # Small rectangle [minLon, minLat, maxLon, maxLat]
    aoi = ee.Geometry.Rectangle([39.55, 24.40, 39.65, 24.50])

    # -------------------------
    # Setup gee_pipeline if needed
    # -------------------------
    gee_pipeline = None
    if use_gee:
        try:
            ee.Initialize()
        except Exception:
            ee.Authenticate()
            ee.Initialize()

        class DummyGEEPipeline:
            def __init__(self, region):
                self.region = region

        gee_pipeline = DummyGEEPipeline(aoi)

    # -------------------------
    # Create analyzer
    # -------------------------
    ta = TerrainAnalysis(data_dir=data_dir, scale=scale, gee_pipeline=gee_pipeline)

    # -------------------------
    # Run products
    # -------------------------
    try:
        dem_fp = ta.get_dem()
        print("DEM:", dem_fp)

        # Flow dir only works if use_gee=True OR file already exists in raw/
        # (raw/merit_flow_dir.tif)
        try:
            flow_dir_fp = ta.get_flow_dir()
            print("Flow dir:", flow_dir_fp)
        except Exception as e:
            print("Flow dir skipped (missing + no GEE):", e)

        slope_deg_fp = ta.get_slope(units="degrees")
        print("Slope (deg):", slope_deg_fp)

        slope_pct_fp = ta.get_slope(units="percent")
        print("Slope (%):", slope_pct_fp)

        aspect_fp = ta.get_aspect()
        print("Aspect:", aspect_fp)

        prof_curv_fp, plan_curv_fp = ta.get_curvature()
        print("Profile curvature:", prof_curv_fp)
        print("Plan curvature:", plan_curv_fp)

        flow_acc_cells_fp = ta.get_flow_accumulation(kind="cells")
        print("Flow accumulation (cells):", flow_acc_cells_fp)

        sca_fp = ta.get_flow_accumulation(kind="sca")
        print("SCA (m²/m):", sca_fp)

        twi_fp = ta.get_twi()
        print("TWI:", twi_fp)

        spi_fp = ta.get_spi()
        print("SPI:", spi_fp)

        tri_fp = ta.get_tri()
        print("TRI:", tri_fp)

        # Optional extras (if you added them)
        rr_fp = ta.get_relative_relief(window=9)
        print("Relative relief:", rr_fp)

        vd_fp = ta.get_valley_depth()
        print("Valley depth:", vd_fp)

        tpi_fp = ta.get_tpi(window=9)
        print("TPI:", tpi_fp)

        conv_fp = ta.get_convergence_index()
        print("Convergence index:", conv_fp)

        print("\nDone. Outputs written under:", data_dir)

    except Exception as e:
        print("\nFAILED:", e)
        print("Tips:")
        print("- If use_gee=False, make sure DEM exists at:")
        print(f"  {os.path.join(data_dir, 'raw', 'merit_dem.tif')}")
        print("- If use_gee=True, ensure EE auth works and AOI is valid.")

