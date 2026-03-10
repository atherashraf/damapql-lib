from dataclasses import dataclass
from typing import Any

import pandas as pd
from fastapi import HTTPException

from app.damapql.dch.models import LayerInfo, DataModel
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from utils.singletons import da_logger


@dataclass
class RasterInfoResult:
    payload: dict[str, Any]


class RasterStatsService:
    @staticmethod
    def get_layer(uuid: str) -> LayerInfo:
        info = LayerInfo.get_layer_info_by_uuid(uuid)
        if not info:
            raise HTTPException(status_code=404, detail="Layer metadata not found")
        if not info.data_model or info.data_model.code != DataModel.R.name:
            raise HTTPException(status_code=400, detail="Requested layer is not a raster model")
        return info

    @staticmethod
    def open_raster(info: LayerInfo) -> RioRaster:
        raster = info.to_raster()
        if raster is None:
            raise HTTPException(status_code=404, detail="Raster file not found")
        return raster

    @staticmethod
    def open_cog(info: LayerInfo) -> COGRaster:
        cog = info.to_cog()
        if cog is None:
            raise HTTPException(status_code=404, detail="COG file not found")
        return cog

    @classmethod
    def get_pixel_value(cls, uuid: str, long: float, lat: float) -> dict[str, Any]:
        info = cls.get_layer(uuid)
        try:
            cog = cls.open_cog(info)
            data = cog.get_pixel_value_at_long_lat(long, lat)
            return {"payload": data}
        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(f"Error extracting pixel value for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Internal processing error")

    @classmethod
    def get_raster_info(cls, uuid: str) -> RasterInfoResult:
        info = cls.get_layer(uuid)

        try:
            raster = cls.open_raster(info)
            band_count = raster.get_spectral_resolution()
            bands_info = {}

            for band_index in range(1, band_count + 1):
                data_array = raster.get_data_array(band_index)
                nodata = raster.get_nodata_value()
                bands_info[band_index] = BandProcess.get_summary_data(data_array, nodata)

            payload = {
                "bandCount": band_count,
                "bandsInfo": bands_info,
                "driver": raster.get_driver_name(),
                "crs": str(raster.get_crs()),
                "nodata": raster.get_nodata_value(),
                "spatialResolution": raster.get_spatial_resolution(),
                "spectralResolution": raster.get_spectral_resolution(),
                "radiometricResolution": raster.get_radiometric_resolution(),
            }
            return RasterInfoResult(payload=payload)

        except HTTPException:
            raise
        except Exception as e:
            da_logger.exception(f"Error extracting raster info for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Internal processing error")

    @classmethod
    def get_attribute_summary_df(cls, uuid: str) -> pd.DataFrame:
        """
        Mirrors the raster branch currently used by /layer_attributes/.
        """
        info = cls.get_layer(uuid)
        raster = cls.open_raster(info)

        spec_res = raster.get_spectral_resolution()

        if spec_res == 1 and info.layer_styling:
            style = info.layer_styling
            if "values" in style:
                values = style["values"]
            else:
                palette = style.get("palette", {})
                values = [int(key) for key in palette.keys()]

            data = raster.get_data_array(1)
            labels = style.get("labels", [])

            spatial_res = raster.get_spatial_resolution(in_meter=True)
            res_in_km = (spatial_res[0] / 1000, spatial_res[1] / 1000)

            res = BandProcess.get_value_area_data(
                data,
                raster.get_nodata_value(),
                res_in_km,
                values,
                labels,
                "sq. km.",
            )
            return pd.DataFrame(res).rename(columns={"area": "area_sq_km"})

        return raster.get_band_summaries()

    @classmethod
    def get_attribute_summary_grid(cls, uuid: str) -> dict[str, Any]:
        df = cls.get_attribute_summary_df(uuid)
        return ManageDataFrameUtils.grid_response(df, pkCols=[])