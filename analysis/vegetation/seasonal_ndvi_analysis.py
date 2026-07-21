from pathlib import Path
from typing import Callable, Dict, Literal, Optional

import ee
import geopandas as gpd

from digitalarzengine.analysis.climate.agro_hydro_climatic_analysis import SeasonType
from digitalarzengine.analysis.season_info import get_season_months
from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.pipeline.gee_pipeline import GEEPipeline
from digitalarzengine.utils.singletons import da_logger


class SeasonalNDVIAnalysis:
    """
    NDVI vegetation activity analysis using MODIS MOD13Q1.

    GEE is initialized lazily only when needed.
    """

    def __init__(
        self, aoi_gdf: gpd.GeoDataFrame,
        start_date, end_date,
        output_dir: str | Path,
        scale: int = 250,
        crs: str = "EPSG:4326",
        config_fp: Optional[str | Path] = None,
    ):
        self.aoi_gdf = aoi_gdf
        self.config_fp = config_fp
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.scale = scale
        self.crs = crs

        self.gee_pipeline: Optional[GEEPipeline] = None

    def get_gee_region(self):
        if self.gee_pipeline is None:
            if self.config_fp is None:
                raise ValueError("config_fp must be provided to initialize GEEPipeline.")
            self.gee_pipeline = GEEPipeline(aoi=self.aoi_gdf, config_fp=str(self.config_fp))
        return self.gee_pipeline.region

    @staticmethod
    def _date_str(date_obj) -> str:
        if hasattr(date_obj, "strftime"):
            return date_obj.strftime("%Y-%m-%d")
        return str(date_obj)

    def get_start_date(self) -> str:
        return self._date_str(self.start_date)

    def get_end_date(self) -> str:
        return self._date_str(self.end_date)


    def _season_filter(self, season: SeasonType) -> ee.Filter:
        months = get_season_months(season)

        return ee.Filter.Or(
            *[ee.Filter.calendarRange(m, m, "month") for m in months]
        )

    def get_modis_ndvi_collection(self) -> ee.ImageCollection:
        region = self.get_gee_region()

        return (
            ee.ImageCollection("MODIS/061/MOD13Q1")
            .filterBounds(region.get_aoi())
            .filterDate(self.get_start_date(), self.get_end_date())
        )

    def prepare_image_for_download(self, img: ee.Image) -> ee.Image:
        region = self.get_gee_region()

        return (
            img.clip(region.get_aoi())
            .resample("bilinear")
            .reproject(crs=self.crs, scale=self.scale)
            .float()
        )

    def annual_or_seasonal_mean(
        self,
        collection: ee.ImageCollection,
        band: str,
        output_name: str,
        season: SeasonType = "annual",
        scale_factor: float = 1.0,
    ) -> ee.Image:
        start_year = ee.Number(ee.Date(self.get_start_date()).get("year"))
        end_year = ee.Number(ee.Date(self.get_end_date()).get("year"))
        years = ee.List.sequence(start_year, end_year)

        def yearly_mean(year):
            year = ee.Number(year)

            if season == "annual":
                start = ee.Date.fromYMD(year, 1, 1)
                end = start.advance(1, "year")
                coll = collection.filterDate(start, end)
            else:
                coll = (
                    collection
                    .filter(ee.Filter.calendarRange(year, year, "year"))
                    .filter(self._season_filter(season))
                )

            return (
                coll.select(band)
                .mean()
                .multiply(scale_factor)
                .rename(output_name)
                .set("year", year)
            )

        return (
            ee.ImageCollection(years.map(yearly_mean))
            .mean()
            .rename(output_name)
            .float()
        )

    def get_ndvi(self, season: SeasonType = "annual") -> ee.Image:
        return self.annual_or_seasonal_mean(
            collection=self.get_modis_ndvi_collection(),
            band="NDVI",
            output_name=f"ndvi_{season}",
            season=season,
            scale_factor=0.0001,
        )

    def get_vegetation_activity_index(
        self,
        season: SeasonType = "annual",
    ) -> ee.Image:
        ndvi = self.get_ndvi(season)

        return (
            ndvi.unitScale(0.05, 0.50)
            .clamp(0, 1)
            .rename(f"vegetation_activity_{season}")
            .float()
        )

    def get_layer_getters(
        self,
        season: SeasonType = "annual",
    ) -> Dict[str, Callable[[], ee.Image]]:
        return {
            f"ndvi_{season}": lambda: self.get_ndvi(season),
            f"vegetation_activity_{season}": lambda: self.get_vegetation_activity_index(season),
        }

    def get_layer(
        self,
        layer_name: str,
        season: SeasonType = "annual",
    ) -> ee.Image:
        getters = self.get_layer_getters(season)

        if layer_name not in getters:
            raise ValueError(
                f"Unknown layer: {layer_name}. "
                f"Available layers: {list(getters.keys())}"
            )

        return getters[layer_name]()

    def download_layer(
        self,
        layer_name: str,
        season: SeasonType = "annual",
        force: bool = False,
        save_metadata: bool = False,
    ) -> Path:
        output_file = self.output_dir / f"{layer_name}.tif"
        da_logger.info(f"Output file: {output_file}")
        if output_file.exists() and not force:
            print(f"Skipping existing: {output_file}")
            return output_file

        if output_file.exists() and force:
            output_file.unlink()

        print(f"Preparing {layer_name}...")

        ee_img = self.prepare_image_for_download(
            self.get_layer(layer_name, season)
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

    def prepare_all_layers(
        self,
        season: SeasonType = "annual",
        force: bool = False,
        save_metadata: bool = False,
    ) -> Dict[str, Path]:
        outputs = {}

        for layer_name in self.get_layer_getters(season).keys():
            outputs[layer_name] = self.download_layer(
                layer_name=layer_name,
                season=season,
                force=force,
                save_metadata=save_metadata,
            )

        return outputs

    def prepare_seasonal_layers(
        self,
        force: bool = False,
        save_metadata: bool = False,
    ) -> Dict[str, Dict[str, Path]]:
        outputs = {}

        for season in ["winter", "spring", "summer", "autumn", "annual"]:
            outputs[season] = self.prepare_all_layers(
                season=season,
                force=force,
                save_metadata=save_metadata,
            )

        return outputs


if __name__ == "__main__":
    aoi_gdf = gpd.read_file("tests/data/aoi.geojson")
    start_date = "2020-01-01"
    end_date = "2025-12-31"
    output = Path("tests/data/vegetation_index")
    analysis = SeasonalNDVIAnalysis(
        aoi_gdf=aoi_gdf,
        start_date=start_date,
        end_date=end_date,
        output_dir=output,
    )

    analysis.prepare_seasonal_layers(force=True)