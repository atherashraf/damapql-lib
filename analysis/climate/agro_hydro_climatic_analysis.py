from pathlib import Path
from typing import Callable, Dict, Literal, Optional

import ee
from geopandas import GeoDataFrame

from digitalarzengine.analysis.season_info import SeasonType, get_season_months
from digitalarzengine.io.gee.gee_image import GEEImage
from digitalarzengine.pipeline.gee_pipeline import GEEPipeline




class AgroHydroClimaticAnalysis:
    """
    Evapotranspiration, climatic water balance, and atmospheric demand analysis.

    Outputs:
      - Actual evapotranspiration ET
      - Potential evapotranspiration PET
      - Precipitation
      - Latent heat flux LE
      - ET/PET ratio
      - Water deficit PET - ET
      - Evaporative Stress Index ESI = 1 - ET/PET
      - Moisture Availability Index MAI = P/PET
      - Climatic Water Balance CWB = P - PET
      - Irrigation Dependency ID = max(PET - P, 0)
      - Temperature
      - Solar radiation
      - Vapor pressure deficit
      - Wind speed
      - Crop water stress proxy
    """

    def __init__(
        self,
        aoi_gdf: GeoDataFrame,
        start_date,
        end_date,
        output_dir: str | Path,
        scale: int = 500,
        crs: str = "EPSG:4326",
        config_fp: Optional[str | Path] = None,
    ):
        self.aoi_gdf = aoi_gdf
        self.gee_pipeline = None
        self.config_fp = config_fp
        # self.gee_pipeline = gee_pipeline
        # self.region = gee_pipeline.region
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.scale = scale
        self.crs = crs

    def get_gee_region(self, config_fp: Optional[str | Path] = None):
        if self.gee_pipeline is None:
            if self.config_fp is None:
                raise ValueError("config_fp must be provided to initialize GEEPipeline.")
            self.gee_pipeline = GEEPipeline(aoi=self.aoi_gdf, config_fp=str(self.config_fp))
        return self.gee_pipeline.region

    # ------------------------------------------------------------------
    # Date helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _date_str(date_obj) -> str:
        if hasattr(date_obj, "strftime"):
            return date_obj.strftime("%Y-%m-%d")
        return str(date_obj)

    def get_start_date(self) -> str:
        return self._date_str(self.start_date)

    def get_end_date(self) -> str:
        return self._date_str(self.end_date)


    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------
    def get_modis_et_collection(self) -> ee.ImageCollection:
        """
        MODIS MOD16A2GF evapotranspiration product.

        Main bands:
          - ET
          - PET
          - LE
          - PLE

        ET and PET scale factor: 0.1
        """
        region = self.get_gee_region()
        return (
            ee.ImageCollection("MODIS/061/MOD16A2GF")
            .filterBounds(region.get_aoi())
            .filterDate(self.get_start_date(), self.get_end_date())
        )

    def get_era5_collection(self) -> ee.ImageCollection:
        """
        ERA5-Land monthly aggregated climate data.

        Used for:
          - precipitation
          - temperature
          - solar radiation
          - vapor pressure deficit
          - wind speed
        """

        return (
            ee.ImageCollection("ECMWF/ERA5_LAND/MONTHLY_AGGR")
            .filterBounds(self.get_gee_region().get_aoi())
            .filterDate(self.get_start_date(), self.get_end_date())
        )

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    def _season_filter(self, season: SeasonType) -> ee.Filter:
        months = get_season_months(season)
        return ee.Filter.Or(
            *[ee.Filter.calendarRange(m, m, "month") for m in months]
        )

    def filter_by_season(
        self,
        collection: ee.ImageCollection,
        season: SeasonType = "annual",
    ) -> ee.ImageCollection:
        if season == "annual":
            return collection
        return collection.filter(self._season_filter(season))

    def prepare_image_for_download(self, img: ee.Image) -> ee.Image:
        return (
            img.clip(self.get_gee_region().get_aoi())
            .resample("bilinear")
            .reproject(crs=self.crs, scale=self.scale)
            .float()
        )

    def annual_or_seasonal_sum(
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

        def yearly_sum(year):
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
                .sum()
                .multiply(scale_factor)
                .rename(output_name)
                .set("year", year)
            )

        return (
            ee.ImageCollection(years.map(yearly_sum))
            .mean()
            .rename(output_name)
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

    def get_actual_et(self, season: SeasonType = "annual") -> ee.Image:
        """
        Actual evapotranspiration.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        return self.annual_or_seasonal_sum(
            self.get_modis_et_collection(),
            "ET",
            f"actual_et_{season}",
            season,
            0.1,
        )

    def get_potential_et(self, season: SeasonType = "annual") -> ee.Image:
        """
        Potential evapotranspiration.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        return self.annual_or_seasonal_sum(
            self.get_modis_et_collection(),
            "PET",
            f"potential_et_{season}",
            season,
            0.1,
        )

    def get_latent_heat_flux(self, season: SeasonType = "annual") -> ee.Image:
        """
        Latent heat flux.

        Unit depends on MODIS LE product convention.
        Aggregation: mean.
        """

        return self.annual_or_seasonal_mean(
            self.get_modis_et_collection(),
            "LE",
            f"latent_heat_flux_{season}",
            season,
            1.0,
        )

    # ------------------------------------------------------------------
    # ERA5 climate variables
    # ------------------------------------------------------------------
    def get_precipitation(self, season: SeasonType = "annual") -> ee.Image:
        """
        Total precipitation from ERA5-Land.

        ERA5-Land precipitation is in meters.
        Converted to millimeters.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        return self.annual_or_seasonal_sum(
            self.get_era5_collection(),
            "total_precipitation_sum",
            f"precipitation_{season}",
            season,
            1000.0,
        )

    def get_temperature(self, season: SeasonType = "annual") -> ee.Image:
        """
        Mean 2m air temperature.

        ERA5 temperature is Kelvin.
        Converted to Celsius.

        Unit: degree Celsius.
        """

        img = self.annual_or_seasonal_mean(
            self.get_era5_collection(),
            "temperature_2m",
            f"temperature_{season}",
            season,
        )
        return img.subtract(273.15).rename(f"temperature_{season}").float()

    def get_solar_radiation(self, season: SeasonType = "annual") -> ee.Image:
        """
        Surface net solar radiation.

        ERA5-Land surface_net_solar_radiation_sum is usually J/m2.
        Converted to MJ/m2.

        Unit: MJ/m2.
        Aggregation: mean of monthly sums.
        """

        return self.annual_or_seasonal_mean(
            self.get_era5_collection(),
            "surface_net_solar_radiation_sum",
            f"solar_radiation_{season}",
            season,
            1.0 / 1_000_000.0,
        )

    def get_wind_speed(self, season: SeasonType = "annual") -> ee.Image:
        """
        Wind speed derived from u and v wind components.

        Method:
          - compute monthly wind speed
          - compute yearly mean
          - compute climatological mean across years

        Unit: m/s.
        """

        collection = self.get_era5_collection()

        start_year = ee.Number(ee.Date(self.get_start_date()).get("year"))
        end_year = ee.Number(ee.Date(self.get_end_date()).get("year"))
        years = ee.List.sequence(start_year, end_year)

        def yearly_wind(year):
            year = ee.Number(year)

            if season == "annual":
                coll = collection.filter(
                    ee.Filter.calendarRange(year, year, "year")
                )
            else:
                coll = (
                    collection
                    .filter(ee.Filter.calendarRange(year, year, "year"))
                    .filter(self._season_filter(season))
                )

            def calc_wind(img):
                u = img.select("u_component_of_wind_10m")
                v = img.select("v_component_of_wind_10m")

                wind = (
                    u.pow(2)
                    .add(v.pow(2))
                    .sqrt()
                    .rename("wind_speed")
                )

                return wind.copyProperties(img, img.propertyNames())

            return (
                coll.map(calc_wind)
                .mean()
                .rename(f"wind_speed_{season}")
                .set("year", year)
            )

        yearly_collection = ee.ImageCollection(years.map(yearly_wind))

        return (
            yearly_collection.mean()
            .rename(f"wind_speed_{season}")
            .float()
        )

    def get_vapor_pressure_deficit(self, season: SeasonType = "annual") -> ee.Image:
        """
        Vapor Pressure Deficit.

        Method:
          - compute monthly VPD
          - compute yearly mean
          - compute climatological mean across years
          - Saturation Vapour Pression es
          - Actual Vapour Pressure ea
        Formula:
          es = 0.6108 * exp((17.27 * T) / (T + 237.3))
          ea = 0.6108 * exp((17.27 * Td) / (Td + 237.3))
          VPD = es - ea

        Unit: kPa.
        """

        collection = self.get_era5_collection()

        start_year = ee.Number(ee.Date(self.get_start_date()).get("year"))
        end_year = ee.Number(ee.Date(self.get_end_date()).get("year"))
        years = ee.List.sequence(start_year, end_year)

        def yearly_vpd(year):
            year = ee.Number(year)

            if season == "annual":
                coll = collection.filter(
                    ee.Filter.calendarRange(year, year, "year")
                )
            else:
                coll = (
                    collection
                    .filter(ee.Filter.calendarRange(year, year, "year"))
                    .filter(self._season_filter(season))
                )

            def calc_vpd(img):
                temp_c = img.select("temperature_2m").subtract(273.15)
                dew_c = img.select("dewpoint_temperature_2m").subtract(273.15)

                es = temp_c.expression(
                    "0.6108 * exp((17.27 * T) / (T + 237.3))",
                    {"T": temp_c},
                )

                ea = dew_c.expression(
                    "0.6108 * exp((17.27 * TD) / (TD + 237.3))",
                    {"TD": dew_c},
                )

                vpd = (
                    es.subtract(ea)
                    .max(0)
                    .rename("vpd")
                )

                return vpd.copyProperties(img, img.propertyNames())

            return (
                coll.map(calc_vpd)
                .mean()
                .rename(f"vpd_{season}")
                .set("year", year)
            )

        yearly_collection = ee.ImageCollection(years.map(yearly_vpd))

        return (
            yearly_collection.mean()
            .rename(f"vpd_{season}")
            .float()
        )
    # ------------------------------------------------------------------
    # Derived ET and water-balance indicators
    # ------------------------------------------------------------------

    def get_et_pet_ratio(self, season: SeasonType = "annual") -> ee.Image:
        et = self.get_actual_et(season)
        pet = self.get_potential_et(season)

        return (
            et.divide(pet.add(0.0001))
            .clamp(0, 1)
            .rename(f"et_pet_ratio_{season}")
            .float()
        )

    def get_water_deficit(self, season: SeasonType = "annual") -> ee.Image:
        """
        Water deficit proxy.

        Formula:
          PET - ET

        Higher values indicate stronger unmet atmospheric demand.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        et = self.get_actual_et(season)
        pet = self.get_potential_et(season)

        return pet.subtract(et).max(0).rename(f"water_deficit_{season}").float()

    def get_evaporative_stress_index(self, season: SeasonType = "annual") -> ee.Image:
        """
               Evaporative Stress Index.

               Formula:
                 ESI = 1 - ET/PET

               Higher values indicate stronger evapotranspiration stress.

               Unit: dimensionless.
               """
        return (
            ee.Image(1)
            .subtract(self.get_et_pet_ratio(season))
            .rename(f"evaporative_stress_index_{season}")
            .float()
        )

    def get_aridity_stress(self, season: SeasonType = "annual") -> ee.Image:
        """
        Aridity stress proxy.

        Same logic as Evaporative Stress Index:
          1 - ET/PET

        Kept for backward compatibility.
        """

        return (
            self.get_evaporative_stress_index(season)
            .rename(f"aridity_stress_{season}")
            .float()
        )

    def get_moisture_availability_index(self, season: SeasonType = "annual") -> ee.Image:
        """
        Moisture Availability Index.

        Formula:
          MAI = P / PET

        Higher values indicate better climatic moisture availability.

        Unit: dimensionless.
        """

        p = self.get_precipitation(season)
        pet = self.get_potential_et(season)

        return (
            p.divide(pet.add(0.0001))
            .rename(f"moisture_availability_index_{season}")
            .float()
        )

    def get_climatic_water_balance(self, season: SeasonType = "annual") -> ee.Image:
        """
        Climatic Water Balance.

        Formula:
          CWB = P - PET

        Positive values indicate water surplus.
        Negative values indicate climatic water deficit.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        p = self.get_precipitation(season)
        pet = self.get_potential_et(season)

        return p.subtract(pet).rename(f"climatic_water_balance_{season}").float()

    def get_irrigation_dependency(self, season: SeasonType = "annual") -> ee.Image:
        """
        Irrigation Dependency Proxy.

        Formula:
          ID = max(PET - P, 0)

        Higher values indicate stronger irrigation requirement.

        Unit:
          annual = mm/year
          seasonal = mm/season
        """

        p = self.get_precipitation(season)
        pet = self.get_potential_et(season)

        return pet.subtract(p).max(0).rename(f"irrigation_dependency_{season}").float()

    def get_crop_water_stress_proxy(self, season: SeasonType = "annual") -> ee.Image:
        """
        Crop water stress proxy.

        Combines:
          - evaporative stress
          - vapor pressure deficit
          - temperature stress

        Output: 0-1 relative stress.
        """

        esi = self.get_evaporative_stress_index(season).clamp(0, 1)
        vpd = self.get_vapor_pressure_deficit(season).unitScale(0, 5).clamp(0, 1)
        temp = self.get_temperature(season).unitScale(20, 45).clamp(0, 1)

        return (
            esi.multiply(0.5)
            .add(vpd.multiply(0.3))
            .add(temp.multiply(0.2))
            .rename(f"crop_water_stress_{season}")
            .float()
        )

    # ------------------------------------------------------------------
    # Layer registry
    # ------------------------------------------------------------------
    def get_layer_getters(
        self,
        season: SeasonType = "annual",
    ) -> Dict[str, Callable[[], ee.Image]]:
        return {
            f"actual_et_{season}": lambda: self.get_actual_et(season),
            f"potential_et_{season}": lambda: self.get_potential_et(season),
            f"precipitation_{season}": lambda: self.get_precipitation(season),
            f"latent_heat_flux_{season}": lambda: self.get_latent_heat_flux(season),
            f"temperature_{season}": lambda: self.get_temperature(season),
            f"solar_radiation_{season}": lambda: self.get_solar_radiation(season),
            f"vpd_{season}": lambda: self.get_vapor_pressure_deficit(season),
            f"wind_speed_{season}": lambda: self.get_wind_speed(season),
            f"et_pet_ratio_{season}": lambda: self.get_et_pet_ratio(season),
            f"water_deficit_{season}": lambda: self.get_water_deficit(season),
            f"evaporative_stress_index_{season}": lambda: self.get_evaporative_stress_index(season),
            f"aridity_stress_{season}": lambda: self.get_aridity_stress(season),
            f"moisture_availability_index_{season}": lambda: self.get_moisture_availability_index(season),
            f"climatic_water_balance_{season}": lambda: self.get_climatic_water_balance(season),
            f"irrigation_dependency_{season}": lambda: self.get_irrigation_dependency(season),
            f"crop_water_stress_{season}": lambda: self.get_crop_water_stress_proxy(season),
        }

    def get_layer(self, layer_name: str, season: SeasonType = "annual") -> ee.Image:
        getters = self.get_layer_getters(season)

        if layer_name not in getters:
            raise ValueError(
                f"Unknown layer: {layer_name}. "
                f"Available layers: {list(getters.keys())}"
            )

        return getters[layer_name]()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------
    def download_layer(
        self,
        layer_name: str,
        season: SeasonType = "annual",
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

        ee_img = self.prepare_image_for_download(self.get_layer(layer_name, season))

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