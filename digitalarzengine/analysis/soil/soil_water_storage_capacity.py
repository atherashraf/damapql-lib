from pathlib import Path
from typing import Callable, Dict, Literal, Optional

import ee
import geopandas as gpd

from app.cli import init_gee_pipeline
from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.pipeline.gee_pipeline import GEEPipeline


DepthBand = Literal["b0", "b10", "b30", "b60", "b100", "b200"]
RootDepth = Literal[30, 60, 100, 200]


class SoilWaterStorageAnalysis:
    """
    Soil water storage and Available Water Capacity analysis
    using OpenLandMap soil texture and SOC products.

    Outputs:
      - theta_1500 per depth
      - theta_33 per depth
      - soil_capacity per depth
      - AWC for 30, 60, 100, 200 cm rooting depths
    """

    DEPTH_LAYERS = {
        "b0": {"depth_cm": "0-10", "thickness_mm": 100},
        "b10": {"depth_cm": "10-30", "thickness_mm": 200},
        "b30": {"depth_cm": "30-60", "thickness_mm": 300},
        "b60": {"depth_cm": "60-100", "thickness_mm": 400},
        "b100": {"depth_cm": "100-200", "thickness_mm": 1000},
    }

    ROOT_DEPTH_LAYERS = {
        30: ["b0", "b10"],
        60: ["b0", "b10", "b30"],
        100: ["b0", "b10", "b30", "b60"],
        200: ["b0", "b10", "b30", "b60", "b100"],
    }

    def __init__(
        self,
        aoi_gdf: gpd.GeoDataFrame,
        output_dir: str | Path,
        scale: int = 250,
        crs: str = "EPSG:4326",
        config_fp: Optional[str | Path] = None,
    ):
        self.aoi_gdf = aoi_gdf
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config_fp = config_fp
        self.scale = scale
        self.crs = crs

        self.gee_pipeline: Optional[GEEPipeline] = None

    def get_gee_region(self):
        if self.gee_pipeline is None:
            if self.config_fp is None:
                raise ValueError("config_fp must be provided to initialize GEEPipeline.")
            self.gee_pipeline = GEEPipeline(aoi=self.aoi_gdf, config_fp=str(self.config_fp))
        return self.gee_pipeline.region

    def prepare_image_for_download(self, img: ee.Image) -> ee.Image:
        region = self.get_gee_region()

        return (
            img.clip(region.get_aoi())
            .resample("bilinear")
            .reproject(crs=self.crs, scale=self.scale)
            .float()
        )

    # --------------------------------------------------
    # OpenLandMap source images
    # --------------------------------------------------

    def get_sand_image(self) -> ee.Image:
        return ee.Image(
            "OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02"
        )

    def get_clay_image(self) -> ee.Image:
        return ee.Image(
            "OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02"
        )

    def get_soc_image(self) -> ee.Image:
        return ee.Image(
            "OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02"
        )

    # --------------------------------------------------
    # Soil inputs
    # --------------------------------------------------

    def get_sand_fraction(self, band: DepthBand) -> ee.Image:
        """
        Sand fraction as decimal fraction.
        OpenLandMap sand is percentage.
        """
        return (
            self.get_sand_image()
            .select(band)
            .divide(100.0)
            .rename(f"sand_{band}")
            .float()
        )

    def get_clay_fraction(self, band: DepthBand) -> ee.Image:
        """
        Clay fraction as decimal fraction.
        OpenLandMap clay is percentage.
        """
        return (
            self.get_clay_image()
            .select(band)
            .divide(100.0)
            .rename(f"clay_{band}")
            .float()
        )

    def get_soc_fraction(self, band: DepthBand) -> ee.Image:
        """
        SOC as decimal fraction.

        NOTE:
        Confirm your OpenLandMap SOC unit.
        If SOC is already in g/kg, divide by 1000.
        If SOC is in percent, divide by 100.

        This uses g/kg -> fraction.
        """
        return (
            self.get_soc_image()
            .select(band)
            .divide(1000.0)
            .rename(f"soc_{band}")
            .float()
        )

    def get_som_fraction(self, band: DepthBand) -> ee.Image:
        """
        Soil Organic Matter using Van Bemmelen factor.
        SOM = 1.724 * SOC
        """
        return (
            self.get_soc_fraction(band)
            .multiply(1.724)
            .rename(f"som_{band}")
            .float()
        )

    # --------------------------------------------------
    # Pedotransfer functions
    # --------------------------------------------------

    def get_theta_1500(self, band: DepthBand) -> ee.Image:
        """
        Permanent Wilting Point at -1500 kPa.
        """
        s = self.get_sand_fraction(band)
        c = self.get_clay_fraction(band)
        om = self.get_som_fraction(band)

        theta_t = (
            s.multiply(-0.024)
            .add(c.multiply(0.487))
            .add(om.multiply(0.006))
            .add(s.multiply(om).multiply(0.005))
            .add(c.multiply(om).multiply(-0.013))
            .add(s.multiply(c).multiply(0.068))
            .add(0.031)
        )

        theta = theta_t.add(
            theta_t.multiply(0.14).subtract(0.002)
        )

        return theta.rename(f"theta_1500_{band}").float()

    def get_theta_33(self, band: DepthBand) -> ee.Image:
        """
        Field Capacity at -33 kPa.
        """
        s = self.get_sand_fraction(band)
        c = self.get_clay_fraction(band)
        om = self.get_som_fraction(band)

        theta_t = (
            s.multiply(-0.251)
            .add(c.multiply(0.195))
            .add(om.multiply(0.011))
            .add(s.multiply(om).multiply(0.006))
            .add(c.multiply(om).multiply(-0.027))
            .add(s.multiply(c).multiply(0.452))
            .add(0.299)
        )

        theta = theta_t.add(
            theta_t.pow(2).multiply(1.283)
            .subtract(theta_t.multiply(0.374))
            .subtract(0.015)
        )

        return theta.rename(f"theta_33_{band}").float()

    def get_soil_capacity_fraction(self, band: DepthBand) -> ee.Image:
        """
        Volumetric plant-available water fraction per soil layer.
        """
        return (
            self.get_theta_33(band)
            .subtract(self.get_theta_1500(band))
            .max(0)
            .rename(f"soil_capacity_{band}")
            .float()
        )

    def get_awc_layer_mm(self, band: DepthBand) -> ee.Image:
        """
        Layer AWC in mm.
        """
        thickness = self.DEPTH_LAYERS[band]["thickness_mm"]

        return (
            self.get_soil_capacity_fraction(band)
            .multiply(thickness)
            .rename(f"awc_{band}_mm")
            .float()
        )

    def get_awc_root_depth(self, depth_cm: RootDepth) -> ee.Image:
        """
        Cumulative AWC in mm for selected rooting depth.
        """
        layers = self.ROOT_DEPTH_LAYERS[depth_cm]

        img = ee.Image(0)

        for band in layers:
            img = img.add(self.get_awc_layer_mm(band))

        return img.rename(f"awc_{depth_cm}cm").float()

    def get_rootzone_buffering_index(self) -> ee.Image:
        """
        Simple normalized root-zone buffering index.

        Uses 0-200 cm AWC and rescales 0-200 mm to 0-1.
        You can adjust range later after seeing results.
        """
        awc_200 = self.get_awc_root_depth(200)

        return (
            awc_200.unitScale(0, 200)
            .clamp(0, 1)
            .rename("rootzone_buffering_index")
            .float()
        )

    # --------------------------------------------------
    # Layer getters
    # --------------------------------------------------

    def get_layer_getters(self) -> Dict[str, Callable[[], ee.Image]]:
        getters = {}

        for band in self.DEPTH_LAYERS.keys():
            getters[f"sand_{band}"] = lambda b=band: self.get_sand_fraction(b)
            getters[f"clay_{band}"] = lambda b=band: self.get_clay_fraction(b)
            getters[f"som_{band}"] = lambda b=band: self.get_som_fraction(b)
            getters[f"theta_1500_{band}"] = lambda b=band: self.get_theta_1500(b)
            getters[f"theta_33_{band}"] = lambda b=band: self.get_theta_33(b)
            getters[f"soil_capacity_{band}"] = lambda b=band: self.get_soil_capacity_fraction(b)
            getters[f"awc_{band}_mm"] = lambda b=band: self.get_awc_layer_mm(b)

        for depth in self.ROOT_DEPTH_LAYERS.keys():
            getters[f"awc_{depth}cm"] = lambda d=depth: self.get_awc_root_depth(d)

        getters["rootzone_buffering_index"] = self.get_rootzone_buffering_index

        return getters

    def get_layer(self, layer_name: str) -> ee.Image:
        # IMPORTANT: initialize Earth Engine before any ee.Image is created
        self.get_gee_region()

        getters = self.get_layer_getters()

        if layer_name not in getters:
            raise ValueError(
                f"Unknown layer: {layer_name}. "
                f"Available layers: {list(getters.keys())}"
            )

        return getters[layer_name]()

    # --------------------------------------------------
    # Download
    # --------------------------------------------------

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

        ee_img = self.prepare_image_for_download(
            self.get_layer(layer_name)
        )

        result = GEEImage(ee_img).download_image(
            file_path=output_file,
            img_region=self.get_gee_region(),
            scale=self.scale,
            save_metadata=save_metadata,
            within_aoi_only=True,
        )

        if not result.ok:
            raise RuntimeError(result.error)

        print(f"Saved: {output_file}")

        return output_file

    def prepare_key_layers(
        self,
        force: bool = False,
        save_metadata: bool = False,
    ) -> Dict[str, Path]:
        """
        Recommended outputs for Chapter 4.
        """
        layer_names = [
            "awc_b0_mm",
            "awc_b10_mm",
            "awc_b30_mm",
            "awc_b60_mm",
            "awc_b100_mm",
            "awc_30cm",
            "awc_60cm",
            "awc_100cm",
            "awc_200cm",
            "rootzone_buffering_index",
        ]

        outputs = {}

        for layer_name in layer_names:
            outputs[layer_name] = self.download_layer(
                layer_name=layer_name,
                force=force,
                save_metadata=save_metadata,
            )

        return outputs

if __name__ == "__main__":
    aoi_gdf = gpd.read_file("tests/data/aoi.geojson")
    output_dir = Path("tests/data/vegetation_index")
    soil_awc = SoilWaterStorageAnalysis(
        aoi_gdf=aoi_gdf,
        output_dir=output_dir,
        scale=250,
    )

    soil_outputs =  soil_awc.prepare_key_layers(force=False)