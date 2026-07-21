import os
import tempfile

import mercantile
import numpy as np
import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import box
from io import BytesIO

import pytest
from digitalarzengine.io.cog_raster import COGRaster, DiscreteStyle, ContinuousStyle

from digitalarzengine.tests.raster.conftest import raster_crs, raster_extent
from digitalarzengine.utils.singletons import da_logger

# Constants
SAMPLE_COG = "data/test_dummy_utm43_cog.tif"

@pytest.fixture(scope="module")
def cog_raster(dummy_raster_path):
    # Ensure COG is created from dummy raster
    if not os.path.exists(SAMPLE_COG):
        COGRaster.create_cog(dummy_raster_path, des_path=SAMPLE_COG)
    return COGRaster.open_from_local(SAMPLE_COG)


def test_01_create_cog_from_geotiff(dummy_raster_path):
    if os.path.exists(SAMPLE_COG):
        os.remove(SAMPLE_COG)
    cog_path = COGRaster.create_cog(dummy_raster_path, des_path=SAMPLE_COG)
    assert os.path.exists(cog_path)


def test_02_open_from_local(cog_raster):
    assert isinstance(cog_raster, COGRaster)
    assert cog_raster.get_file_path() == SAMPLE_COG


def test_03_get_pixel_value_at_coords(cog_raster):
    west, south, east, north = cog_raster.bounds
    mid_x = (west + east) / 2
    mid_y = (south + north) / 2

    # Transform from UTM (EPSG:32643) to WGS84 (EPSG:4326)
    transformer = Transformer.from_crs(raster_crs, "EPSG:4326", always_xy=True)
    mid_lon, mid_lat = transformer.transform(mid_x, mid_y)

    val = cog_raster.get_pixel_value_at_long_lat(mid_lon, mid_lat)
    assert val is not None


def test_04_read_tile_png(cog_raster):
    # Initialize transformer
    transformer = Transformer.from_crs(raster_crs, "EPSG:4326", always_xy=True)

    # Transform all four corners (minx, miny), (maxx, maxy)
    min_lon, min_lat = transformer.transform(raster_extent[0], raster_extent[1])
    max_lon, max_lat = transformer.transform(raster_extent[2], raster_extent[3])

    # Use transformed bounds in mercantile
    for tile in mercantile.tiles(min_lon, min_lat, max_lon, max_lat, zooms=18):
        da_logger.debug(f"x:{tile.x}, y:{tile.y}, z:{tile.z}")
        colormap = COGRaster.create_color_map({"min":0, "max":1, "palette":{"#00ff00", "#ff0000"}})
        tile = cog_raster.read_tile_as_png(tile.x, tile.y, tile.z, colormap)
        assert isinstance(tile, BytesIO)
        assert tile.getbuffer().nbytes > 0
        break


def test_05_create_empty_image(cog_raster):
    empty = cog_raster.create_empty_image(256, 256, format="JPEG")
    assert isinstance(empty, BytesIO)
    assert empty.getbuffer().nbytes > 0


def test_06_create_colormap():
    # Test 1: Continuous Style
    style_cont = ContinuousStyle(
        type="continuous",
        min=0,
        max=100,
        palette=["#0000FF", "#00FF00", "#FF0000"]
    )
    cmap_cont = COGRaster.create_color_map(style_cont)

    assert isinstance(cmap_cont, list)
    assert len(cmap_cont) == 3  # 2 intervals + overflow
    for rng, color in cmap_cont:
        assert isinstance(rng, tuple) and len(rng) == 2
        assert isinstance(color, tuple) and len(color) == 4
        assert all(isinstance(c, int) for c in color)

    # Test 2: Discrete Style
    style_disc = DiscreteStyle(
        type="discrete",
        values=[0, 25, 50, 75, 100],
        palette={
            0: "#0000FF",
            1: "#00FF00",
            2: "#FFFF00",
            3: "#FFA500",
            4: "#FF0000"
        },
        min_val=0,
        max_val=100
    )
    cmap_disc = COGRaster.create_color_map(style_disc)

    assert isinstance(cmap_disc, list)
    assert len(cmap_disc) == 5
    for rng, color in cmap_disc:
        assert isinstance(rng, tuple) and len(rng) == 2
        assert isinstance(color, tuple) and len(color) == 4
        assert all(isinstance(c, int) for c in color)


def test_07_create_color_map_with_style():
    style = {
        'min': 0,
        'max': 100,
        'palette': ['#000000', '#888888', '#FFFFFF']
    }
    cmap = COGRaster.create_color_map(style)
    assert isinstance(cmap, list)
    for rng, color in cmap:
        assert isinstance(rng, tuple) and isinstance(color, tuple)


def test_08_raster_from_array_and_multiband(cog_raster):
    data = np.random.randint(0, 255, (3, 256, 256), dtype=np.uint8)
    mask = np.ones((256, 256), dtype=bool)
    extent = [0, 0, 10, 10]
    raster = cog_raster.raster_from_array(data, mask, extent)
    assert raster.get_dataset() is not None


def test_09_save_tile_as_geotiff(cog_raster):
    with tempfile.NamedTemporaryFile(suffix=".tif") as tmp:
        cog_raster.save_tile_as_geotiff(0, 0, 0, tmp.name)
        assert os.path.exists(tmp.name)


def test_10_read_data_under_aoi(cog_raster):
    aoi = gpd.GeoDataFrame(geometry=[box(*cog_raster.bounds)], crs="EPSG:4326")
    result = cog_raster.read_data_under_aoi(aoi)
    assert isinstance(result, type(cog_raster.get_rio_raster()))



def test_11_get_tile_coordinates_at_zoom(cog_raster):
    """
    Tests the new get_tile_coordinates_at_zoom method.
    """
    # Use a realistic zoom level for testing
    test_zoom = 15
    tile_coords = cog_raster.get_tile_coordinates_at_zoom(zoom=test_zoom)
    da_logger.debug(f"tile coords: {tile_coords}")
    # The list of tile coordinates should not be empty
    assert isinstance(tile_coords, list)
    assert len(tile_coords) > 0

    # Check that each item in the list is a tuple of two integers (tile_x, tile_y)
    first_tile = tile_coords[0]
    assert isinstance(first_tile, tuple)
    assert len(first_tile) == 2
    assert isinstance(first_tile[0], int)
    assert isinstance(first_tile[1], int)

    test_tile_x, test_tile_y = tile_coords[0]
    # Call the method to get xyz values for that tile
    all_xyz_data = cog_raster.get_tile_values_at_tile_coords(test_tile_x, test_tile_y, test_zoom)
    da_logger.debug(f"tile value: {all_xyz_data}")
    # The list should not be empty
    assert all_xyz_data is not None
    assert len(all_xyz_data) > 0

    # The number of returned tuples should equal the number of pixels in a single tile
    expected_size = 256 * 256
    assert len(all_xyz_data) == expected_size

    # Check the data type and format of the first element
    first_element = all_xyz_data[0]
    assert isinstance(first_element, tuple)
    assert len(first_element) == 3
    assert all(isinstance(val, float) for val in first_element)
