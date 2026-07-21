import os
import numpy as np
import rasterio
import mercantile
# import pyproj
# import shapely
from io import BytesIO
# from typing import Union, List, Optional, Tuple
from PIL import Image
from rasterio.windows import from_bounds
from rasterio.enums import Resampling
from rasterio.session import AWSSession
from rasterio.warp import transform_bounds
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

from digitalarzengine.io.file_io import FileIO
from digitalarzengine.utils.singletons import da_logger
from digitalarzengine.io.rio_raster import RioRaster
from digitalarzengine.processing.operations.transformation_ops import TransformationOperations
from digitalarzengine.utils.styling.raster_styling import ColorMapBase, ContinuousStyle, DiscreteStyle, StyleLike


class COGRaster:
    """
    Refactored COG Handler using Rasterio & Rio-Cogeo.
    Removed rio-tiler to resolve pyproj version conflicts.
    """

    def __init__(self, fp: str, s3_session=None):
        self.file_path = fp
        self.s3_session = s3_session
        self._env = rasterio.Env(AWSSession(s3_session)) if "s3://" in fp else rasterio.Env()

        with self._env:
            with rasterio.open(self.file_path) as src:
                self.bounds = src.bounds
                self.crs = src.crs
                self.meta = src.meta
                self.nodata = src.nodata
                self.count = src.count
                # Get stats from the smallest overview to save memory/bandwidth
                self.global_minmax = self._compute_approx_minmax(src)

    def _compute_approx_minmax(self, src):
        try:
            # Read the last overview (smallest resolution) for speed
            ovw_index = src.overviews(1)
            if ovw_index:
                data = src.read(1, overview=len(ovw_index) - 1, masked=True)
            else:
                data = src.read(1, masked=True)
            return float(data.min()), float(data.max())
        except Exception as e:
            da_logger.warning(f"Min/Max fallback: {e}")
            return 0, 255

    @classmethod
    def open_cog(cls, fp: str, s3_session=None) -> 'COGRaster':
        return cls(fp, s3_session=s3_session)

    def read_tile_as_png(self, x: int, y: int, z: int, color_map=None, tile_size: int = 256) -> BytesIO:
        """
        Manually fetch an XYZ tile using Rasterio Windows.
        """
        try:
            # 1. Get tile bounds in WGS84
            tile_bounds = mercantile.bounds(x, y, z)  # (west, south, east, north)

            # 2. Project bounds to Dataset CRS
            with self._env:
                with rasterio.open(self.file_path) as src:
                    # Transform tile bounds to match raster CRS
                    dst_bounds = transform_bounds("EPSG:4326", src.crs, *tile_bounds)

                    # 3. Create a Window and Read
                    window = from_bounds(*dst_bounds, transform=src.transform)

                    # Read data (bands, height, width)
                    data = src.read(
                        out_shape=(src.count, tile_size, tile_size),
                        window=window,
                        resampling=Resampling.bilinear,
                        boundless=True  # Handles tiles partially outside the raster
                    )

                    mask = src.dataset_mask(window=window, out_shape=(tile_size, tile_size), boundless=True)

            # 4. Processing for PNG
            if src.count == 1:
                # Normalize and apply colormap logic if needed
                # For simplicity, returning a grayscale-to-RGBA conversion
                img_data = self._apply_custom_colormap(data[0], mask, color_map)
                return self.create_image(img_data)

            # RGB/RGBA Handling
            rgb = data[:3].transpose(1, 2, 0)
            alpha = mask[:, :, np.newaxis]
            rgba = np.concatenate([rgb, alpha], axis=2).astype(np.uint8)
            return self.create_image(rgba)

        except Exception as e:
            da_logger.error(f"Tile error {x},{y},{z}: {e}")
            return self.create_empty_image(tile_size, tile_size)

    def _apply_custom_colormap(self, data, mask, color_map):
        """
        Mimics rio-tiler's colormap rendering.
        """
        # Simple implementation: Convert 2D data to 4D RGBA based on your style rules
        # You would integrate your style.to_cog_color_map() logic here
        h, w = data.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)

        # Default grayscale logic if no colormap
        norm_data = ((data - self.global_minmax[0]) / (self.global_minmax[1] - self.global_minmax[0]) * 255).astype(
            np.uint8)
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = norm_data
        rgba[..., 3] = mask
        return rgba

    @staticmethod
    def create_image(np_array, format="PNG") -> BytesIO:
        img = Image.fromarray(np_array)
        buffer = BytesIO()
        img.save(buffer, format=format)
        buffer.seek(0)
        return buffer

    @staticmethod
    def create_empty_image(size_x, size_y):
        blank = np.zeros((size_x, size_y, 4), dtype=np.uint8)
        return COGRaster.create_image(blank)

    @classmethod
    def create_cog(cls, src_path: str, dst_path: str = None, profile: str = "deflate") -> str:
        """
        Replaced with rio-cogeo for reliable COG generation.
        """
        if dst_path is None:
            filename, _ = FileIO.get_file_name_ext(src_path)
            dst_path = os.path.join(os.path.dirname(src_path), f"{filename}_cog.tif")

        cog_profiles_dict = cog_profiles.get(profile)

        cog_translate(
            src_path,
            dst_path,
            cog_profiles_dict,
            in_memory=False,
            quiet=True
        )
        da_logger.info(f"COG Created: {dst_path}")
        return dst_path

    def get_pixel_value_at_long_lat(self, lon: float, lat: float):
        with self._env:
            with rasterio.open(self.file_path) as src:
                # Sample returns a generator
                for val in src.sample([(lon, lat)]):
                    return val.tolist()
        return None