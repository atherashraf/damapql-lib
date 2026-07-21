from pathlib import Path
from typing import Dict, Optional

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

from digitalarzengine.io.rio_raster import RioRaster


class HydroEcologicalBridgeAnalysis:
    """
    Local raster-based hydro-ecological bridge analysis for AEZ.

    This class computes integrated climate–soil indicators from local GeoTIFFs.
    It does not require Google Earth Engine.

    Indicators
    ----------
    1. Water Sufficiency Index (WSI)

        WSI = P_annual(cm) / AWC_0-200(cm)

    where:
        P_annual  = annual rainfall depth
        AWC_0-200 = cumulative Available Water Capacity within 0–200 cm root zone

    Interpretation:
        WSI compares annual rainfall input with soil root-zone storage capacity.

        Low WSI:
            rainfall input is small relative to storage capacity.
        WSI ≈ 1:
            rainfall approximately matches root-zone storage capacity.
        High WSI:
            rainfall exceeds storage capacity, suggesting possible runoff,
            drainage, or episodic water surplus depending on infiltration.

    2. Effective Rainfall Storage Ratio (ERSR)

        Equation
        --------
        ERSR = AWC_0-200(cm) / P_annual(cm)

        where:
            AWC_0-200 = cumulative Available Water Capacity
                         within the 0–200 cm root zone

            P_annual  = annual rainfall depth

        Interpretation:
            ERSR describes the relative capacity of soils to retain and
            buffer rainfall within deeper root-zone environments.

            High ERSR:
                strong soil-water storage relative to rainfall input,
                indicating greater environmental moisture persistence
                and hydro-ecological buffering potential.

            Moderate ERSR:
                balanced rainfall-storage relationship with moderate
                environmental moisture retention capacity.

            Low ERSR:
                limited storage capacity relative to rainfall input,
                suggesting more episodic environmental moisture behavior,
                rapid depletion, or reduced buffering efficiency.

    3. Water Buffering Index (WBI)

        WBI = (P_annual / (PET + 1)) × RBI

    where:
        PET = annual potential evapotranspiration
        RBI = Root-Zone Soil-Water Buffering Index

    Interpretation:
        WBI combines rainfall supply, atmospheric demand, and root-zone buffering.
        This indicator is more appropriate after PET is introduced in Chapter 6.

    Notes
    -----
    - These indicators are relative AEZ indicators, not irrigation scheduling tools.
    - All inputs are automatically aligned to the rainfall raster grid.
    - Rainfall and AWC should be in the same unit before ratio calculation.
      This class converts mm to cm internally where needed.
    """

    def __init__(
        self,
        rainfall_annual_fp: str | Path,
        awc_200_fp: str | Path,
        output_dir: str | Path,
        *,
        pet_annual_fp: Optional[str | Path] = None,
        rbi_fp: Optional[str | Path] = None,
        rainfall_unit: str = "mm",
        awc_unit: str = "mm",
        pet_unit: str = "mm",
        nodata: float = -9999.0,
        epsilon: float = 1e-6,
    ):
        self.rainfall_annual_fp = Path(rainfall_annual_fp)
        self.awc_200_fp = Path(awc_200_fp)
        self.pet_annual_fp = Path(pet_annual_fp) if pet_annual_fp else None
        self.rbi_fp = Path(rbi_fp) if rbi_fp else None

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.rainfall_unit = rainfall_unit.lower()
        self.awc_unit = awc_unit.lower()
        self.pet_unit = pet_unit.lower()

        self.nodata = nodata
        self.epsilon = epsilon



    @staticmethod
    def _to_cm(arr: np.ndarray, unit: str) -> np.ndarray:
        unit = unit.lower()

        if unit == "cm":
            return arr.astype("float32")

        if unit == "mm":
            return (arr / 10.0).astype("float32")

        if unit == "m":
            return (arr * 100.0).astype("float32")

        raise ValueError(
            f"Unsupported unit: {unit}. Use 'mm', 'cm', or 'm'."
        )

    def _save_array_like(
            self,
            template_raster: RioRaster,
            arr: np.ndarray,
            output_name: str,
    ) -> Path:
        output_fp = self.output_dir / output_name

        arr = arr.astype("float32")
        arr[~np.isfinite(arr)] = np.nan

        out_raster = template_raster.rio_raster_from_array(arr)
        out_raster.save_to_file(str(output_fp))

        return output_fp

    def compute_wsi(
        self,
        output_name: str = "water_sufficiency_index.tif",
        clip_range: Optional[tuple[float, float]] = None,
    ) -> Path:
        """
        Compute Water Sufficiency Index.

        Equation:
            WSI = P_annual(cm) / AWC_0-200(cm)

        Output:
            GeoTIFF with dimensionless WSI values.
        """
        output_fp = self.output_dir / output_name
        if output_fp.exists():
            return output_fp
        awc_200_raster = RioRaster(self.awc_200_fp)
        rainfall_raster = RioRaster(self.rainfall_annual_fp)
        rainfall_raster = rainfall_raster.make_coincident(awc_200_raster, resampling="bilinear")
        rainfall = rainfall_raster.get_data_array(1)
        awc_200 = awc_200_raster.get_data_array(1)

        rainfall_cm = self._to_cm(rainfall, self.rainfall_unit)
        awc_cm = self._to_cm(awc_200, self.awc_unit)

        wsi = rainfall_cm / (awc_cm + self.epsilon)

        if clip_range is not None:
            wsi = np.clip(wsi, clip_range[0], clip_range[1])

        return self._save_array_like(
            template_raster=rainfall_raster,
            arr=wsi,
            output_name=output_name,
        )

    def compute_ersr(
            self,
            output_name: str = "effective_rainfall_storage_ratio.tif",
            clip_range: Optional[tuple[float, float]] = None,
    ) -> Path:
        """
        Compute Effective Rainfall Storage Ratio.

        ERSR = AWC_0-200(cm) / P_annual(cm)
        """
        output_fp = self.output_dir / output_name
        if output_fp.exists():
            return output_fp
        awc_200_raster = RioRaster(self.awc_200_fp)
        rainfall_raster = RioRaster(self.rainfall_annual_fp)

        rainfall_raster = rainfall_raster.make_coincident(
            awc_200_raster,
            resampling="bilinear",
        )

        rainfall = rainfall_raster.get_data_array(1)
        awc_200 = awc_200_raster.get_data_array(1)

        rainfall_cm = self._to_cm(rainfall, self.rainfall_unit)
        awc_cm = self._to_cm(awc_200, self.awc_unit)

        rainfall_cm[~np.isfinite(rainfall_cm)] = np.nan
        awc_cm[~np.isfinite(awc_cm)] = np.nan
        rainfall_cm[rainfall_cm <= 0] = np.nan
        awc_cm[awc_cm < 0] = np.nan

        ersr = awc_cm / (rainfall_cm + self.epsilon)

        if clip_range is not None:
            ersr = np.clip(ersr, clip_range[0], clip_range[1])

        return self._save_array_like(
            template_raster=rainfall_raster,
            arr=ersr,
            output_name=output_name,
        )

    def compute_wbi(
            self,
            output_name: str = "water_buffering_index.tif",
            clip_range: Optional[tuple[float, float]] = None,
    ) -> Path:
        """
        Compute Water Buffering Index.

        WBI = (P_annual / (PET + 1)) × RBI
        """
        if self.pet_annual_fp is None:
            raise ValueError("pet_annual_fp is required to compute WBI.")

        if self.rbi_fp is None:
            raise ValueError("rbi_fp is required to compute WBI.")

        rainfall_raster = RioRaster(self.rainfall_annual_fp)
        pet_raster = RioRaster(self.pet_annual_fp)
        rbi_raster = RioRaster(self.rbi_fp)

        pet_raster = pet_raster.make_coincident(
            rainfall_raster,
            resampling="bilinear",
        )

        rbi_raster = rbi_raster.make_coincident(
            rainfall_raster,
            resampling="bilinear",
        )

        rainfall = rainfall_raster.get_data_array(1)
        pet = pet_raster.get_data_array(1)
        rbi = rbi_raster.get_data_array(1)

        rainfall_mm = (
            rainfall.astype("float32")
            if self.rainfall_unit == "mm"
            else self._to_cm(rainfall, self.rainfall_unit) * 10.0
        )

        pet_mm = (
            pet.astype("float32")
            if self.pet_unit == "mm"
            else self._to_cm(pet, self.pet_unit) * 10.0
        )

        rainfall_mm[~np.isfinite(rainfall_mm)] = np.nan
        pet_mm[~np.isfinite(pet_mm)] = np.nan
        rbi[~np.isfinite(rbi)] = np.nan

        pet_mm[pet_mm < 0] = np.nan
        rbi[(rbi < 0) | (rbi > 1)] = np.nan

        wbi = (rainfall_mm / (pet_mm + 1.0)) * rbi

        if clip_range is not None:
            wbi = np.clip(wbi, clip_range[0], clip_range[1])

        return self._save_array_like(
            template_raster=rainfall_raster,
            arr=wbi,
            output_name=output_name,
        )

    def compute_all(
        self,
        include_wsi: bool = True,
        include_ersr: bool = True,
        include_wbi: bool = False,
    ) -> Dict[str, Path]:
        outputs = dict()
        if include_wsi:
            outputs["water_sufficiency_index"] = self.compute_wsi()
        if include_ersr:
            outputs["effective_rainfall_storage_ratio"] = self.compute_ersr()
        if include_wbi:
            outputs["water_buffering_index"] = self.compute_wbi()

        return outputs