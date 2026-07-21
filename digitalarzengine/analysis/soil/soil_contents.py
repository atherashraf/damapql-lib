import os
import requests
import traceback
import numpy as np
from pathlib import Path
from typing import Union, List
from tqdm import tqdm

# Engine Imports
from digitalarzengine.io.file_io import FileIO
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger


class SoilContent:
    @classmethod
    def get_clay_content(cls, des_dir: Union[str, Path], lat_lim: List[float], lon_lim: List[float], level='sl1'):
        """Downloads SoilGrids Clay Content data using tqdm for progress tracking."""
        output_folder = Path(des_dir) / 'SoilGrids' / 'Clay_Content'
        FileIO.mkdirs(str(output_folder))

        fp_end = output_folder / f'ClayContentMassFraction_{level}_SoilGrids_percentage.tif'

        if not fp_end.exists():
            # Replacement for WaitBarConsole
            print(f"Initializing Clay Content download for level: {level}...")
            cls.download_data(output_folder, lat_lim, lon_lim, "CLAY", level)
        else:
            da_logger.info(f"Clay Content map already exists at {fp_end}")

    @classmethod
    def get_silt_content(cls, des_dir: Union[str, Path], lat_lim: List[float], lon_lim: List[float], level='sl1'):
        """Downloads SoilGrids Silt Content data."""
        output_folder = Path(des_dir) / 'SoilGrids' / 'Silt_Content'
        FileIO.mkdirs(str(output_folder))

        fp_end = output_folder / f'SiltContentMassFraction_{level}_SoilGrids_percentage.tif'

        if not fp_end.exists():
            print(f"Initializing Silt Content download for level: {level}...")
            cls.download_data(output_folder, lat_lim, lon_lim, "SILT", level)
        else:
            da_logger.info(f"Silt Content map already exists at {fp_end}")

    @staticmethod
    def get_soil_data_level() -> dict:
        return {
            "sl1": "0-5", "sl2": "5-15", "sl3": "15-30",
            "sl4": "30-60", "sl5": "60-100", "sl6": "100-200"
        }

    @staticmethod
    def get_soil_dataset_info(dataset_key=None) -> dict:
        parameters_info = {
            "BULKDENSITY": {"name": "BulkDensity", "parameter": "bdod", "unit": "cg/cm3", "conversion_unit": "g/cm3",
                            "conversion": 10},
            "NITROGEN": {"name": "Nitrogen", "parameter": "nitrogen", "unit": "cg/kg", "conversion_unit": "g/kg",
                         "conversion": 0.01},
            "SOC": {"name": "SoilOrganicCarbonContent", "parameter": "soc", "unit": "dg/kg", "conversion_unit": "g/kg",
                    "conversion": 0.1},
            "SOD": {"name": "SoilOrganicCarbonDensity", "parameter": "ocd", "unit": "g/dm3", "conversion_unit": None,
                    "conversion": None},
            "SOM": {"name": "SoilOrganicMatterContent", "parameter": "soc", "unit": "dg/kg", "conversion_unit": "g/kg",
                    "conversion": 0.1 * 1.724},
            "PH": {"name": "SoilPH", "parameter": "phh2o", "unit": "pH10", "conversion_unit": None, "conversion": None},
            "CLAY": {"name": "ClayContentMassFraction", "parameter": "clay", "unit": "g/kg",
                     "conversion_unit": "percentage", "conversion": 0.1},
            "SAND": {"name": "SandContentMassFraction", "parameter": "sand", "unit": "g/kg",
                     "conversion_unit": "percentage", "conversion": 0.1},
            "SILT": {"name": "SiltContentMassFraction", "parameter": "silt", "unit": "g/kg",
                     "conversion_unit": "percentage", "conversion": 0.1},
        }
        if dataset_key and dataset_key.upper() not in parameters_info:
            raise ValueError(f"{dataset_key} doesn't exist in soil contents info")
        return parameters_info[dataset_key.upper()] if dataset_key else parameters_info

    @classmethod
    def download_data(cls, output_folder: Path, lat_lim: List[float], lon_lim: List[float], dataset: str, level: str,
                      apply_conversion=True):
        dict_levels = cls.get_soil_data_level()
        dataset = dataset.upper()
        info = cls.get_soil_dataset_info(dataset)

        orig_fp = Path(cls.get_file_path(level, dataset, False, str(output_folder)))
        FileIO.mkdirs(str(orig_fp.parent))

        try:
            if not orig_fp.exists():
                parameter = info["parameter"]
                level_str = dict_levels[level]

                url = (f"https://maps.isric.org/mapserv?map=/map/{parameter}.map&SERVICE=WCS&VERSION=2.0.1"
                       f"&REQUEST=GetCoverage&COVERAGEID={parameter}_{level_str}cm_mean&FORMAT=image/tiff"
                       f"&SUBSET=long({lon_lim[0]},{lon_lim[1]})&SUBSET=lat({lat_lim[0]},{lat_lim[1]})"
                       f"&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
                       f"&OUTPUTCRS=http://www.opengis.net/def/crs/EPSG/0/4326")

                response = requests.get(url, stream=True)
                if response.status_code == 200:
                    total_size = int(response.headers.get('content-length', 0))
                    # tqdm integrated for data streaming
                    with open(orig_fp, "wb") as f, tqdm(
                            total=total_size,
                            unit='iB',
                            unit_scale=True,
                            desc=f"Downloading {dataset} ({level})"
                    ) as pbar:
                        for chunk in response.iter_content(1024):
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))
                else:
                    da_logger.error(f"Download failed: {response.status_code}")
                    return None

            if apply_conversion and info.get('conversion'):
                conv_factor = info['conversion']
                final_fp = Path(cls.get_file_path(level, dataset, True, str(output_folder)))

                if not final_fp.exists():
                    print(f"Applying conversion ({conv_factor})...")
                    raster = RioRaster(str(orig_fp))
                    data = raster.get_data_array(1, convert_no_data_2_nan=True)

                    data_converted = data.astype(np.float32) * conv_factor
                    data_final = BandProcess.gap_filling(data_converted, nodata_value=0)

                    new_raster = RioRaster.raster_from_array(
                        data_final.astype(np.float32),
                        crs=raster.get_crs(),
                        g_transform=raster.get_geo_transform(),
                        nodata_value=0
                    )
                    new_raster.save_to_file(str(final_fp))
                    return str(final_fp)

            return str(orig_fp)

        except Exception as e:
            da_logger.error(f"Error in download_data: {e}")
            traceback.print_exc()

    @classmethod
    def get_file_path(cls, level: str, dataset: str, apply_conversion: bool, output_folder: str = None) -> str:
        info = cls.get_soil_dataset_info(dataset)
        dict_levels = cls.get_soil_data_level()
        unit = info['conversion_unit'] if (apply_conversion and info.get('conversion')) else info['unit']
        unit_str = unit.replace("/", "-") if unit else ""

        lvl_tag = dict_levels.get(level, level)
        fn = f"{info['name']}_{lvl_tag}_{unit_str}.tif"

        return str(Path(output_folder) / fn) if output_folder else fn