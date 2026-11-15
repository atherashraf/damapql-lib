import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.damapql.dch.dtypes import AttributeTablePayload
from app.damapql.dch.models import LayerInfo, DataModel
from app.damapql.dch.services.layer_accessibility.raster_access_service import RasterAccessService
from app.damapql.dch.utils.df_utils import ManageDataFrameUtils
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger
from digitalarzengine.utils.styling.raster_styling import ContinuousStyle


@dataclass
class RasterInfoResult:
    payload: dict[str, Any]


class RasterStatsService:

    # ============================================================
    # LAYER
    # ============================================================

    @staticmethod
    def get_layer(uuid: str, ) -> LayerInfo:

        info = LayerInfo.get_layer_info_by_uuid(uuid)

        if not info:
            raise HTTPException(status_code=404, detail="Layer metadata not found", )

        if (not info.data_model or info.data_model.code != DataModel.R.name):
            raise HTTPException(status_code=400, detail="Requested layer is not a raster model", )

        return info

    # ============================================================
    # STYLE VALUE HELPER
    # ============================================================

    @staticmethod
    def _get_style_values(style: dict[str, Any], ) -> list[Any]:
        """
        Returns raster class values from layer styling.

        Priority:
        1. style["values"]
        2. palette keys

        Palette keys are converted to int/float when possible.
        """

        if "values" in style:
            return style["values"]

        values = []

        for key in style.get("palette", {}, ).keys():

            try:
                number = float(key)

                if number.is_integer():
                    values.append(int(number))
                else:
                    values.append(number)

            except (TypeError, ValueError,):
                values.append(key)

        return values

    # ============================================================
    # PIXEL VALUE
    # ============================================================

    @classmethod
    def get_pixel_value(cls, uuid: str, long: float, lat: float, minio_manager: MinioManager | None = None, ) -> dict[
        str, Any]:

        info = cls.get_layer(uuid)

        raster = None

        try:
            raster = RasterAccessService.open_cog(info, minio_manager, )

            data = (raster.get_pixel_value_at_long_lat(long, lat, ))

            return {"payload": data}

        except HTTPException:
            raise

        except Exception as e:

            da_logger.exception(f"Error extracting pixel value "
                                f"for uuid={uuid}: {e}")

            raise HTTPException(status_code=500, detail="Internal workers error", )

        finally:
            if raster is not None:
                try:
                    raster.close()
                except Exception:
                    pass

    # ============================================================
    # RASTER INFO
    # ============================================================

    @classmethod
    def get_raster_info(
            cls,
            uuid: str,
            minio_manager: MinioManager | None = None,
            session: Session | None = None,
    ) -> RasterInfoResult:
        info = (
            LayerInfo.get_layer_info_by_uuid(uuid, session=session)
            if session is not None
            else cls.get_layer(uuid)
        )
        if not info:
            raise HTTPException(status_code=404, detail="Layer metadata not found")
        if not info.data_model or info.data_model.code != DataModel.R.name:
            raise HTTPException(status_code=400, detail="Requested layer is not a raster model")

        raster = None

        try:
            cog_raster = RasterAccessService.open_cog(info, minio_manager)
            raster = cog_raster.get_rio_raster()

            band_count = raster.get_spectral_resolution()
            nodata = raster.get_nodata_value()

            bands_info: dict[int, Any] = {}
            for band_index in range(1, band_count + 1):
                data_array = raster.get_data_array(band_index)
                bands_info[band_index] = BandProcess.get_summary_data(data_array, nodata)

            payload = {
                "bandCount": band_count,
                "bandsInfo": bands_info,
                "driver": raster.get_driver_name(),
                "crs": str(raster.get_crs()),
                "nodata": nodata,
                "spatialResolution": raster.get_spatial_resolution(),
                "spectralResolution": band_count,
                "radiometricResolution": raster.get_radiometric_resolution(),
            }

            return RasterInfoResult(payload=payload)

        except HTTPException:
            raise

        except Exception as e:
            da_logger.exception(f"Error extracting raster info for uuid={uuid}: {e}")
            raise HTTPException(status_code=500, detail="Internal workers error")

        finally:
            if raster is not None:
                try:
                    raster.close()
                except Exception:
                    pass

    # ============================================================
    # ATTRIBUTE META
    # ============================================================

    @classmethod
    def get_attribute_meta(cls, uuid: str, minio_manager: MinioManager | None = None, ) -> dict[str, Any]:

        info = cls.get_layer(uuid)

        raster = None

        try:
            raster = RasterAccessService.open_cog(info, minio_manager, )

            spec_res = (raster.get_spectral_resolution())

            # ----------------------------------------------------
            # Classified single-band raster
            # ----------------------------------------------------

            if (spec_res == 1 and info.layer_styling):

                style = (info.layer_styling)

                values = (cls._get_style_values(style))

                labels = style.get("labels", [], )

                total_count = (len(labels) if labels else len(values))

            # ----------------------------------------------------
            # Multi-band / continuous raster
            # ----------------------------------------------------

            else:

                no_of_summary_stats = 8

                total_count = (no_of_summary_stats * spec_res)

            return {"columns": [], "total_count": total_count, "pkCols": [], }

        except HTTPException:
            raise

        except Exception as e:

            da_logger.exception(f"Error extracting attribute meta "
                                f"for uuid={uuid}: {e}")

            raise HTTPException(status_code=500, detail=("Failed to calculate raster "
                                                         "attribute metadata"), )

        finally:
            if raster is not None:
                try:
                    raster.close()
                except Exception:
                    pass

    # ============================================================
    # ATTRIBUTE SUMMARY DATAFRAME
    # ============================================================

    @classmethod
    def get_attribute_summary_df(cls, uuid: str, minio_manager: MinioManager | None = None, ) -> pd.DataFrame:

        info = cls.get_layer(uuid)

        raster = None

        try:
            cog_raster = RasterAccessService.open_cog(info, minio_manager, )
            raster = cog_raster.get_rio_raster()
            spec_res = (raster.get_spectral_resolution())

            # ----------------------------------------------------
            # Classified single-band raster
            # ----------------------------------------------------

            if (spec_res == 1 and info.layer_styling):
                style = (info.layer_styling)

                values = (cls._get_style_values(style))

                labels = style.get("labels", [], )

                data = (raster.get_data_array(1))

                result = (BandProcess.get_value_area_data(data, raster.get_nodata_value(),
                    raster.get_spatial_resolution(in_meter=True), values, labels, unit="m2", to_km2=True,
                    include_percent=True, ))

                return pd.DataFrame(result)

            # ----------------------------------------------------
            # Multi-band / continuous raster
            # ----------------------------------------------------

            return (raster.get_band_summaries())

        except HTTPException:
            raise

        except Exception as e:

            da_logger.exception(f"Error extracting raster attribute "
                                f"summary for uuid={uuid}: {e}")

            raise HTTPException(status_code=500, detail=("Failed to calculate raster "
                                                         "attribute summary"), )

        finally:
            if raster is not None:
                try:
                    raster.close()
                except Exception:
                    pass

    # ============================================================
    # ATTRIBUTE SUMMARY GRID
    # ============================================================

    @classmethod
    def get_attribute_summary_grid(cls, uuid: str,
            minio_manager: MinioManager | None = None, ) -> AttributeTablePayload:

        df = cls.get_attribute_summary_df(uuid=uuid, minio_manager=minio_manager, )

        return ManageDataFrameUtils.grid_response(df, pkCols=[], )

    @classmethod
    def apply_initial_symbology(
            cls,
            *,
            uuid: str,
            symbology: str | dict[str, Any],
            minio_manager: MinioManager | None = None,
            session: Session | None = None,
    ) -> dict[str, Any] | None:
        """
        Resolves min/max bounds using `get_raster_info` when omitted, validates
        against `ContinuousStyle`, and persists `LayerInfo.layer_styling`.
        """
        raw_style = json.loads(symbology) if isinstance(symbology, str) else dict(symbology)
        if not isinstance(raw_style, dict):
            return None

        palette = raw_style.get("palette") or raw_style.get("colors")
        if not palette:
            return None

        stretch = raw_style.pop("stretch", "minmax")
        band_index = int(raw_style.pop("band", 1) or 1)

        if "min" not in raw_style or "max" not in raw_style:
            info_res = cls.get_raster_info(
                uuid=uuid,
                minio_manager=minio_manager,
                session=session,
            )
            bands_info = info_res.payload.get("bandsInfo") or {}
            band_stats = (
                    bands_info.get(band_index)
                    or bands_info.get(str(band_index))
                    or next(iter(bands_info.values()), {})
            )

            if stretch == "percentile_2_98":
                raw_min = (
                    band_stats.get("p2")
                    if band_stats.get("p2") is not None
                    else band_stats.get("percentile_2")
                )
                raw_max = (
                    band_stats.get("p98")
                    if band_stats.get("p98") is not None
                    else band_stats.get("percentile_98")
                )
                vmin = raw_min if raw_min is not None else band_stats.get("min", 0.0)
                vmax = raw_max if raw_max is not None else band_stats.get("max", 1.0)
            else:
                vmin = band_stats.get("min") if band_stats.get("min") is not None else 0.0
                vmax = band_stats.get("max") if band_stats.get("max") is not None else 1.0

            vmin = float(vmin)
            vmax = float(vmax)
            if vmax <= vmin:
                vmax = vmin + 1.0

            raw_style["min"] = vmin
            raw_style["max"] = vmax

        validated_style = ContinuousStyle(
            type="continuous",
            min=float(raw_style["min"]),
            max=float(raw_style["max"]),
            palette=list(palette),
        ).model_dump()

        # layer_info = LayerInfo.get_layer_info_by_uuid(uuid, session=session)
        # if layer_info is not None:
        #     layer_info.layer_styling = validated_style
        #     layer_info.save(session=session)

        with LayerInfo.managed_session(session=session) as s:
            layer_info = s.execute(
                select(LayerInfo).where(LayerInfo.uuid == uuid)
            ).scalar_one_or_none()
            if layer_info is not None:
                layer_info.layer_styling = validated_style
                s.flush()
                if session is not None:
                    s.commit()

        return validated_style
