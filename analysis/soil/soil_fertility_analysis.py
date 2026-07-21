from pathlib import Path
from typing import Callable, Dict, Optional

import ee

from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.pipeline.gee_pipeline import GEEPipeline
from digitalarzengine.utils.singletons import da_logger


class SoilFertilityAnalysis:
    """
    Soil fertility layers for agro-business / AEZ analysis.

    Outputs:
      - pH
      - organic matter
      - total nitrogen
      - available phosphorus proxy
      - exchangeable potassium proxy
      - salinity EC proxy
      - sodicity SAR proxy
    """

    def __init__(
        self,
        gee_pipeline: GEEPipeline,
        output_dir: str | Path,
        scale: int = 250,
        crs: str = "EPSG:4326",
    ):
        self.gee_pipeline = gee_pipeline
        self.region = gee_pipeline.region
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.scale = scale
        self.crs = crs

    # ------------------------------------------------------------------
    # SoilGrids helper
    # ------------------------------------------------------------------
    @staticmethod
    def root_zone_mean(
        img: ee.Image,
        prefix: str,
        band_name: str,
        include_layers: bool = True,
    ) -> ee.Image:
        bands = [
            f"{prefix}_0-5cm_mean",
            f"{prefix}_5-15cm_mean",
            f"{prefix}_15-30cm_mean",
        ]

        layer_names = [
            f"{band_name}_0_5",
            f"{band_name}_5_15",
            f"{band_name}_15_30",
        ]

        layers = img.select(bands).rename(layer_names)

        mean_band = (
            layers
            .reduce(ee.Reducer.mean())
            .rename(f"{band_name}_mean")
            .float()
        )

        if include_layers:
            return layers.addBands(mean_band).float()

        return mean_band

    # ------------------------------------------------------------------
    # Data source methods
    # ------------------------------------------------------------------
    def get_ph_data(self) -> ee.Image:
        img = ee.Image("projects/soilgrids-isric/phh2o_mean").divide(10)
        return self.root_zone_mean(img, "phh2o", "ph")

    def get_organic_matter(self) -> ee.Image:
        soc = ee.Image("projects/soilgrids-isric/soc_mean").divide(10)
        om = soc.multiply(1.724)
        return self.root_zone_mean(om, "soc", "organic_matter")

    def get_total_N(self) -> ee.Image:
        img = ee.Image("projects/soilgrids-isric/nitrogen_mean").divide(100)
        return self.root_zone_mean(img, "nitrogen", "total_N")


    def get_available_P(self) -> ee.Image:
        """
        KSA-safe phosphorus proxy.
        """

        ph = self.get_ph_data().select("ph_mean")
        om = self.get_organic_matter().select("organic_matter_mean")

        ph_score = (
            ee.Image(1)
            .subtract(ph.subtract(7).abs().divide(3))
            .clamp(0, 1)
        )

        om_score = (
            om.unitScale(5, 40)
            .clamp(0, 1)
        )

        return (
            ph_score.multiply(0.6)
            .add(om_score.multiply(0.4))
            .rename("available_P")
            .float()
        )

    def get_exchangeable_k(self) -> ee.Image:
        """
        KSA-safe exchangeable potassium proxy.
        """

        clay = ee.Image("projects/soilgrids-isric/clay_mean").divide(10)

        clay_root = self.root_zone_mean(
            clay,
            "clay",
            "clay",
        )

        clay_mean = clay_root.select("clay_mean")
        om_mean = self.get_organic_matter().select("organic_matter_mean")

        clay_score = (
            clay_mean
            .unitScale(5, 60)
            .clamp(0, 1)
        )

        om_score = (
            om_mean
            .unitScale(5, 40)
            .clamp(0, 1)
        )

        return (
            clay_score.multiply(0.7)
            .add(om_score.multiply(0.3))
            .rename("exchangeable_k")
            .float()
        )

    def get_shared_environmental_layers(self) -> dict:
        """Helper to fetch and process shared soil layers to avoid redundant GEE calls."""
        # 1. Load the ENTIRE multi-band image asset so deeper bands like 'clay_5-15cm_mean' exist
        clay_multiband = ee.Image("projects/soilgrids-isric/clay_mean")

        # 2. Divide by 10 to scale ALL bands from g/kg to percentage
        clay_pct = clay_multiband.divide(10)

        # 3. Pass the full multi-band percentage image to your root zone calculator
        # (Your internal function will now find 'clay_5-15cm_mean' perfectly!)
        clay_mean = self.root_zone_mean(clay_pct, "clay", "clay").select("clay_mean")

        # 4. Score it for KSA contrast
        clay_score = clay_mean.unitScale(0, 40).clamp(0, 1)

        return {
            "clay_score": clay_score
        }

    # def get_ndsi_surface(self, start_date: str="2021-01-01", end_date: str="2025-12-31"):
    #     s2 = (
    #         ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
    #         .filterBounds(self.region.get_aoi())
    #         .filterDate(start_date, end_date)
    #         .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10))
    #         .median()
    #     )
    #
    #     nir = s2.select("B8")
    #     swir = s2.select("B11")
    #
    #     ndsi = (
    #         swir.subtract(nir)
    #         .divide(swir.add(nir))
    #         .unitScale(-0.5, 0.5)
    #         .clamp(0, 1)
    #     )
    #     return ndsi

    def get_ndsi_surface(self, start_year: int = 2024, end_year: int = 2025):

        aoi = self.region.get_aoi()

        def yearly_ndsi(year):
            year = ee.Number(year)
            start = ee.Date.fromYMD(year, 1, 1)
            end = start.advance(1, "year")

            s2 = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(start, end)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10))
                .select(["B8", "B11"])
                .median()
                .clip(aoi)
            )

            nir = s2.select("B8").toFloat()
            swir = s2.select("B11").toFloat()

            return (
                swir.subtract(nir)
                .divide(swir.add(nir))
                .rename("NDSI")
                .toFloat()
            )

        years = ee.List.sequence(start_year, end_year)

        ndsi_collection = ee.ImageCollection.fromImages(
            years.map(yearly_ndsi)
        )

        ndsi = (
            ndsi_collection
            .median()
            .unitScale(-0.5, 0.5)
            .clamp(0, 1)
            .rename("NDSI")
            .toFloat()
        )

        return ndsi

    def get_salinity_ec(self, shared_layers: dict = None) -> ee.Image:
        """
        Improved EC / salinity proxy for KSA.
        EC_proxy = 0.5 * NDSI + 0.3 * Clay_score + 0.2 * OM_stress
        """
        if shared_layers is None:
            shared_layers = self.get_shared_environmental_layers()

        # 1. Fetch Sentinel-2 Imagery
        # s2 = (
        #     ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        #     .filterBounds(self.region.get_aoi())
        #     .filterDate("2021-01-01", "2025-12-31")
        #     .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 10))
        #     .median()
        # )
        #
        # nir = s2.select("B8")
        # swir = s2.select("B11")
        #
        # ndsi = (
        #     swir.subtract(nir)
        #     .divide(swir.add(nir))
        #     .unitScale(-0.5, 0.5)
        #     .clamp(0, 1)
        # )
        ndsi = self.get_ndsi_surface()

        # 2. Get Organic Matter Stress
        om_mean = self.get_organic_matter().select("organic_matter_mean")

        # Low OM means higher salinity vulnerability
        om_stress = ee.Image(1).subtract(
            om_mean.unitScale(5, 40).clamp(0, 1)
        )

        # 3. Combine weights
        ec_proxy = (
            ndsi.multiply(0.5)
            .add(shared_layers["clay_score"].multiply(0.3))
            .add(om_stress.multiply(0.2))
            .rename("salinity_ec")
            .float()
        )

        return ec_proxy

    def get_sodicity_sar(self) -> ee.Image:
        shared_layers = self.get_shared_environmental_layers()

        ec_proxy = (
            self.get_salinity_ec(shared_layers=shared_layers)
            .clamp(0, 1)
            .toFloat()
        )

        clay_score = (
            shared_layers["clay_score"]
            .clamp(0, 1)
            .toFloat()
        )

        ph_mean = self.get_ph_data().select("ph_mean").toFloat()

        ph_alkalinity = (
            ph_mean
            .subtract(7)
            .divide(2)
            .clamp(0, 1)
            .toFloat()
        )

        return (
            ec_proxy.multiply(0.5)
            .add(clay_score.multiply(0.3))
            .add(ph_alkalinity.multiply(0.2))
            .clamp(0, 1)
            .rename("sodicity_sar_proxy")
            .toFloat()
        )


    # ------------------------------------------------------------------
    # Layer registry
    # ------------------------------------------------------------------
    def get_layer_getters(self) -> Dict[str, Callable[[], ee.Image]]:
        return {
            "ph": self.get_ph_data,
            "organic_matter": self.get_organic_matter,
            "salinity_ec": self.get_salinity_ec,
            "sodicity_sar": self.get_sodicity_sar,
            "total_N": self.get_total_N,
            "available_P": self.get_available_P,
            "exchangeable_k": self.get_exchangeable_k,
        }

    def get_layer(self, layer_name: str) -> ee.Image:
        getters = self.get_layer_getters()

        if layer_name not in getters:
            raise ValueError(
                f"Unknown soil fertility layer: {layer_name}. "
                f"Available layers: {list(getters.keys())}"
            )

        return getters[layer_name]()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def prepare_image_for_download(self, ee_img: ee.Image) -> ee.Image:
        aoi = self.region.get_aoi()

        return (
            ee_img
            .clip(aoi)
            .resample("bilinear")
            .reproject(
                crs=self.crs,
                scale=self.scale,
            )
            .float()
        )

    def download_layer(
        self,
        layer_name: str,
        force: bool = False,
        save_metadata: bool = False,
    ) -> Path:
        output_file = self.output_dir / f"{layer_name}.tif"

        if output_file.exists() and not force:
            print(f"Skipping existing: {output_file}")
            return output_file

        if output_file.exists() and force:
            output_file.unlink()

        print(f"Preparing {layer_name}...")

        ee_img = self.get_layer(layer_name)
        ee_img = self.prepare_image_for_download(ee_img)

        result = GEEImage(ee_img).download_image(
            file_path=output_file,
            img_region=self.region,
            scale=self.scale,
            save_metadata=save_metadata,
            within_aoi_only=True,
        )
        if not result.ok:
            raise RuntimeError(result.error)
        print(f"Saved: {output_file}")
        return output_file

    def prepare_all_layers(
        self,
        force: bool = False,
        save_metadata: bool = False,
    ) -> Dict[str, Path]:
        outputs = {}

        for layer_name in self.get_layer_getters().keys():
            outputs[layer_name] = self.download_layer(
                layer_name,
                force=force,
                save_metadata=save_metadata,
            )

        return outputs

if __name__ == "__main__":
    # gee_pipeline = init_gee_pipeline(aoi_gdf)
    gee_pipeline = None # use to initialize gee_pipline
    # output_dir = OUTPUT_DIR / "output" / "soil_fertility"
    output_dir = "ouput"  #proivde path returned
    soil_fertility = SoilFertilityAnalysis(
        gee_pipeline=gee_pipeline,
        output_dir=output_dir,
        scale=250,
        crs="EPSG:4326",
    )

    soil_fertility.prepare_all_layers(
        force=False,
        save_metadata=False,
    )