from pathlib import Path
from typing import Dict, Optional

import numpy as np

from catalog.geo_utils import GeoCatalog, LayerName
from digitalarzengine.io.rio_raster import RioRaster

"""
    Input:
        Extract data using Soil Fertility Analysis using
            oil_fertility = SoilFertilityAnalysis(
                    gee_pipeline=gee_pipeline,
                    output_dir=output_dir,
                    scale=250,
                    crs="EPSG:4326",
                )

            soil_fertility.prepare_all_layers(
                force=False,
                save_metadata=False,
            )
    Output:
        Build soil fertility suitability index from already downloaded rasters.

        All outputs are 0–1:
          1 = better fertility / suitability
          0 = poor fertility / constraint
        Usage:
            soil_suitability = SoilFertilitySuitability(
                soil_fertility_dir=r"D:\DevProjects\mrda\agri_bussiness\data\output\output\soil_fertility"
            )

            outputs = soil_suitability.prepare_all()

            print(outputs)
"""

class SoilFertilitySuitability:
    def __init__(
        self,
        soil_fertility_dir: str | Path,
        output_dir: Optional[str | Path] = None,
    ):
        self.soil_fertility_dir = Path(soil_fertility_dir)
        self.output_dir = Path(output_dir) if output_dir else self.soil_fertility_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.paths = {
            "ph": self.soil_fertility_dir / "ph.tif",
            "organic_matter": self.soil_fertility_dir / "organic_matter.tif",
            "total_N": self.soil_fertility_dir / "total_N.tif",
            "available_P": self.soil_fertility_dir / "available_P.tif",
            "exchangeable_k": self.soil_fertility_dir / "exchangeable_k.tif",
            "salinity_ec": self.soil_fertility_dir / "salinity_ec.tif",
            "sodicity_sar": self.soil_fertility_dir / "sodicity_sar.tif",
        }

    # ------------------------------------------------------------------
    # Basic raster helpers
    # ------------------------------------------------------------------
    def _read_raster(
            self,
            key: str,
            band_name: Optional[str] = None,
            band_index: int = 1,
            template: Optional[RioRaster] = None,
            resampling: str = "bilinear",
    ) -> tuple[RioRaster, np.ndarray]:

        fp = self.paths[key]

        if not fp.exists():
            raise FileNotFoundError(f"Missing raster for {key}: {fp}")

        raster = RioRaster(str(fp))
        aoi_gdf = GeoCatalog().get_aoi(LayerName.AOI)
        raster.clip_raster(aoi_gdf, in_place=True)

        if template is not None:
            raster = raster.make_coincident(
                template,
                resampling=resampling,
                in_place=False,
            )

        if band_name:
            data = raster.get_data_array_by_band_name(
                [band_name],
                convert_no_data_2_nan=True,
            )
        else:
            data = raster.get_data_array(
                band_index,
                convert_no_data_2_nan=True,
            )

        data = np.squeeze(data).astype(np.float32)

        return raster, data

    @staticmethod
    def _linear_score(
        data: np.ndarray,
        min_value: float,
        max_value: float,
        inverse: bool = False,
    ) -> np.ndarray:
        score = (data - min_value) / (max_value - min_value)
        score = np.clip(score, 0, 1)

        if inverse:
            score = 1 - score

        return score.astype(np.float32)

    @staticmethod
    def _triangular_score(
        data: np.ndarray,
        low: float,
        optimum_low: float,
        optimum_high: float,
        high: float,
    ) -> np.ndarray:
        rising = (data - low) / (optimum_low - low)
        falling = (high - data) / (high - optimum_high)

        score = np.minimum(np.minimum(rising, 1), falling)
        score = np.clip(score, 0, 1)

        return score.astype(np.float32)

    @staticmethod
    def _safe_0_1(data: np.ndarray) -> np.ndarray:
        return np.clip(data, 0, 1).astype(np.float32)

    def _save_like(
        self,
        reference: RioRaster,
        data: np.ndarray,
        output_fp: Path,
        band_names: list[str],
    ) -> Path:
        data = data.astype(np.float32)

        reference.save_to_file(
            str(output_fp),
            data=data,
            crs=reference.get_crs(),
            affine_transform=reference.get_geo_transform(),
            nodata_value=np.nan,
            band_names=band_names,
        )

        return output_fp

    # ------------------------------------------------------------------
    # Individual score layers
    # ------------------------------------------------------------------
    def get_ph_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, ph = self._read_raster(
            "ph",
            band_name="ph_mean",
            template=template,
        )

        # Treat invalid pH values as nodata
        ph = ph.astype("float32")
        ph[(ph <= 0) | (~np.isfinite(ph))] = np.nan

        score = self._triangular_score(
            ph,
            low=6.5,
            optimum_low=7.2,
            optimum_high=8.2,
            high=8.8,
        )

        # Preserve nodata mask
        score[~np.isfinite(ph)] = np.nan

        return raster, score

    def get_organic_matter_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, om = self._read_raster(
            "organic_matter",
            band_name="organic_matter_mean",
            template=template,
        )

        score = self._linear_score(
            om,
            min_value=5,
            max_value=40,
        )

        return raster, score

    def get_total_N_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, total_n = self._read_raster(
            "total_N",
            band_name="total_N_mean",
            template=template,
        )

        score = self._linear_score(
            total_n,
            min_value=0.2,
            max_value=2.5,
        )

        return raster, score

    def get_available_P_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, p = self._read_raster(
            "available_P",
            band_index=1,
            template=template,
        )

        return raster, self._safe_0_1(p)

    def get_exchangeable_k_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, k = self._read_raster(
            "exchangeable_k",
            band_index=1,
            template=template,
        )

        return raster, self._safe_0_1(k)

    def get_salinity_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, salinity_risk = self._read_raster(
            "salinity_ec",
            band_index=1,
            template=template,
        )

        score = 1 - self._safe_0_1(salinity_risk)

        return raster, score.astype(np.float32)

    def get_sodicity_score(self, template: Optional[RioRaster] = None) -> tuple[RioRaster, np.ndarray]:
        raster, sodicity_risk = self._read_raster(
            "sodicity_sar",
            band_index=1,
            template=template,
        )

        score = 1 - self._safe_0_1(sodicity_risk)

        return raster, score.astype(np.float32)


    # ------------------------------------------------------------------
    # Stack and weighted overlay
    # ------------------------------------------------------------------
    def calculate_score_stack(
            self,
            output_name: str = "soil_fertility_scores.tif",
    ) -> Path:
        output_fp = self.output_dir / output_name
        if output_fp.exists():
            return output_fp
        # reference/template raster
        reference, ph = self.get_ph_score()


        _, om = self.get_organic_matter_score(template=reference)
        _, total_n = self.get_total_N_score(template=reference)
        _, p = self.get_available_P_score(template=reference)
        _, k = self.get_exchangeable_k_score(template=reference)
        _, salinity = self.get_salinity_score(template=reference)
        _, sodicity = self.get_sodicity_score(template=reference)

        arrays = [
            ph,
            om,
            total_n,
            p,
            k,
            salinity,
            sodicity,
        ]

        min_rows = min(arr.shape[0] for arr in arrays)
        min_cols = min(arr.shape[1] for arr in arrays)

        arrays = [
            arr[:min_rows, :min_cols]
            for arr in arrays
        ]

        stack = np.stack(arrays, axis=0).astype(np.float32)



        return self._save_like(
            reference=reference,
            data=stack,
            output_fp=output_fp,
            band_names=[
                "ph_score",
                "organic_matter_score",
                "total_N_score",
                "available_P_score",
                "exchangeable_k_score",
                "salinity_score",
                "sodicity_score",
            ],
        )

    def calculate_soil_fertility_index(
            self,
            output_name: str = "soil_fertility_index.tif",
            weights: Optional[Dict[str, float]] = None,
    ) -> Path:
        output_fp = self.output_dir / output_name
        if output_fp.exists():
            return output_fp
        if weights is None:
            weights = {
                "ph": 0.15,
                "organic_matter": 0.20,
                "total_N": 0.20,
                "available_P": 0.15,
                "exchangeable_k": 0.15,
                "salinity": 0.10,
                "sodicity": 0.05,
            }

        reference, ph = self.get_ph_score()

        _, om = self.get_organic_matter_score(template=reference)
        _, total_n = self.get_total_N_score(template=reference)
        _, p = self.get_available_P_score(template=reference)
        _, k = self.get_exchangeable_k_score(template=reference)
        _, salinity = self.get_salinity_score(template=reference)
        _, sodicity = self.get_sodicity_score(template=reference)

        arrays = [ph, om, total_n, p, k, salinity, sodicity]

        min_rows = min(arr.shape[0] for arr in arrays)
        min_cols = min(arr.shape[1] for arr in arrays)

        ph, om, total_n, p, k, salinity, sodicity = [
            arr[:min_rows, :min_cols]
            for arr in arrays
        ]

        total_weight = sum(weights.values())

        sfi = (
                      ph * weights["ph"]
                      + om * weights["organic_matter"]
                      + total_n * weights["total_N"]
                      + p * weights["available_P"]
                      + k * weights["exchangeable_k"]
                      + salinity * weights["salinity"]
                      + sodicity * weights["sodicity"]
              ) / total_weight

        sfi = np.clip(sfi, 0, 1).astype(np.float32)



        return self._save_like(
            reference=reference,
            data=sfi,
            output_fp=output_fp,
            band_names=["soil_fertility_index"],
        )

    def prepare_all(self) -> Dict[str, Path]:
        return {
            "soil_fertility_scores": self.calculate_score_stack(),
            "soil_fertility_index": self.calculate_soil_fertility_index(),
        }

if __name__ == "__main__":
    soil_fertility_dir = r"D:\DevProjects\mrda\agri_bussiness\data\output\output\soil_fertility"
    soil_suitability = SoilFertilitySuitability(
        soil_fertility_dir= soil_fertility_dir
    )
    outputs = soil_suitability.prepare_all()
    print(outputs)