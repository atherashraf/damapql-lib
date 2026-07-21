import numpy as np
import pytest
import geopandas as gpd
from shapely.geometry import Polygon
from affine import Affine

from digitalarzengine.processing.raster.band_process import BandProcess
from digitalarzengine.utils.singletons import da_logger


# ---------- Fixtures ----------
@pytest.fixture
def small_int_raster():
    # 3x4 categorical raster with two classes: 1 and 2
    return np.array([
        [1, 1, 2, 2],
        [1, 2, 2, 2],
        [1, 1, 1, 2],
    ], dtype=np.uint8)

@pytest.fixture
def small_float_raster():
    # A small float raster with a nodata sentinel
    # Shape 3x4
    a = np.array([
        [0.1, 0.2, 0.3, 9.999],   # last one is nodata
        [0.15, 0.22, 0.31, 0.4],
        [0.05, 0.25, 0.35, 0.45],
    ], dtype=np.float32)
    return a

@pytest.fixture
def nodata_value():
    return 9.999

@pytest.fixture
def affine_1m():
    # 1x1 units (e.g. meters) with north-up
    return Affine.translation(100, 200) * Affine.scale(1, -1)


# ---------- reclassify_band ----------
def test_reclassify_band_basic():
    arr = np.array([[0.01, 0.02, 0.28, 0.08]], dtype=np.float32)
    thresholds = {
        "water": (('lt', 0.015), 4),
        "built-up": ((0.015, 0.02), 1),
        "barren": ((0.07, 0.27), 2),
        "vegetation": (('gt', 0.27), 3),
    }
    out = BandProcess.reclassify_band(arr, thresholds, nodata=0)
    # Expected class ids per thresholds: [4,1,3,2]
    assert out.dtype == np.uint8
    assert out.tolist() == [[4, 1, 3, 2]]


# ---------- create_binary_mask ----------
def test_create_binary_mask_int(small_int_raster):
    m = BandProcess.create_binary_mask(small_int_raster, class_value=2)
    assert m.dtype == np.uint8
    assert m.sum() == (small_int_raster == 2).sum()

def test_create_binary_mask_float():
    arr = np.array([[0.1, 0.2, np.nan], [0.1000001, 0.5, 0.1]], dtype=np.float32)
    m = BandProcess.create_binary_mask(arr, class_value=0.1, tol=1e-4)
    # NaNs should never match
    assert m[0, 2] == 0
    # Both 0.1 and 0.1000001 should match within tol
    assert m.sum() == 3


# ---------- raster_2_polygon ----------
def test_raster_2_polygon_returns_polygons(small_int_raster, affine_1m):
    gdf = BandProcess.raster_2_polygon(
        band_data=small_int_raster,
        classes=[1, 2],
        transform=affine_1m,
        crs=3857,
        simplify_tol=0.0
    )
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert set(gdf.columns) == {"class", "geometry"}
    assert gdf.crs.to_epsg() == 3857
    assert all(isinstance(geom, Polygon) for geom in gdf.geometry)
    # Should only contain requested classes
    assert set(gdf["class"].unique().tolist()).issubset({1, 2})
    # There must be at least one polygon for each class in this tiny raster
    assert {1, 2}.issubset(set(gdf["class"].unique().tolist()))


# ---------- get_summary_data ----------
def test_get_summary_data_ignores_nodata_and_negatives(small_float_raster, nodata_value):
    stats = BandProcess.get_summary_data(
        small_float_raster.copy(),
        nodata=nodata_value,
        ignore_negative_values=True,
        ignore_values=[]
    )
    # Basic keys present
    for k in ["mean", "median", "std", "min", "q25", "q75", "max", "sum"]:
        assert k in stats

    # Make sure nodata didn't skew min/max (nodata was 9.999)
    assert stats["max"] < 9.999

def test_get_summary_data_all_nan_returns_nans(nodata_value):
    arr = np.full((2, 3), nodata_value, dtype=np.float32)
    stats = BandProcess.get_summary_data(arr, nodata=nodata_value)
    assert all(np.isnan(v) for v in stats.values())


# ---------- get_value_count_data ----------
def test_get_value_count_data_exact_counts_with_labels(small_int_raster):
    values = [1, 2]
    labels = ["class-1", "class-2"]
    out = BandProcess.get_value_count_data(
        small_int_raster, no_data=None, values=values, value_labels=labels
    )
    da_logger.info(f"res {out}")
    # Counts should match exact equality
    c1 = (small_int_raster == 1).sum()
    c2 = (small_int_raster == 2).sum()

    # Order should align with values
    assert out[0]["value"] == "1"
    assert out[0]["count"] == c1
    assert out[0]["label"] == "class-1"

    assert out[1]["value"] == "2"
    assert out[1]["count"] == c2
    assert out[1]["label"] == "class-2"

def test_get_value_count_data_float_ranges_with_callable_label(small_float_raster, nodata_value):
    # Provide explicit bins => create ranges; last is upper-only
    values = [0.1, 0.2, 0.3, 0.5]
    labeler = lambda v: f"bin:{v}"

    out = BandProcess.get_value_count_data(
        small_float_raster.copy(),
        no_data=nodata_value,
        values=values,
        value_labels=labeler
    )
    # Expect 4 bins transformed to: (0.1,), (0.1,0.2), (0.2,0.3), (0.3,)
    assert len(out) == 4
    # Labels produced for string keys like "<= x", "x - y", ">= x"
    assert all("label" in item for item in out)
    # Total count equals number of non-nodata elements
    total = sum(item["count"] for item in out)
    assert total == np.count_nonzero(~np.isnan(np.where(small_float_raster == nodata_value, np.nan, small_float_raster)))


# ---------- get_value_area_data ----------
def test_get_value_area_data_tuple_res_and_dict_labels(small_int_raster):
    values = [1, 2]
    # spatial_res as (dx, dy) => area_per_pixel = dx*dy
    spatial_res = (10, 20)  # 200 square units per pixel
    labels = { "1": "Class One", "2": "Class Two" }

    out = BandProcess.get_value_area_data(
        data=small_int_raster,
        no_data=None,
        spatial_res=spatial_res,
        values=values,
        value_labels=labels,
        unit="sq. u"
    )
    assert len(out) == 2
    # Check areas
    c1 = int((small_int_raster == 1).sum())
    c2 = int((small_int_raster == 2).sum())
    assert out[0]["value"] == "1"
    assert out[0]["area"] == c1 * 200
    assert out[0]["unit"] == "sq. u"
    assert out[0]["label"] == "Class One"

    assert out[1]["value"] == "2"
    assert out[1]["area"] == c2 * 200
    assert out[1]["unit"] == "sq. u"
    assert out[1]["label"] == "Class Two"

def test_get_value_area_data_scalar_res_and_list_labels(small_int_raster):
    # spatial_res as scalar => treat as area per pixel directly
    spatial_res = 5.0
    values = [1, 2]
    # labels = {'1': "one", '2': "two"}
    labels = ["one", "two"]

    out = BandProcess.get_value_area_data(
        data=small_int_raster,
        no_data=None,
        spatial_res=spatial_res,
        values=values,
        value_labels=labels,
        unit="sq. unit"
    )
    da_logger.info(f"res: {out}")
    c1 = (small_int_raster == 1).sum()
    c2 = (small_int_raster == 2).sum()

    assert out[0]["area"] == c1 * 5.0
    assert out[1]["area"] == c2 * 5.0
    # unit omitted when None
    # assert "unit" not in out[0]
    # assert "unit" not in out[1]


# ---------- get_boolean_raster ----------
def test_get_boolean_raster_multi_value(small_int_raster):
    r = BandProcess.get_boolean_raster(small_int_raster, pixel_value=[1, 2])
    assert r.dtype == np.uint8
    assert r.sum() == small_int_raster.size  # all pixels are 1 or 2

    r2 = BandProcess.get_boolean_raster(small_int_raster, pixel_value=[2])
    assert r2.sum() == (small_int_raster == 2).sum()


# ---------- create_distance_raster ----------
def test_create_distance_raster_small():
    # Simple pattern: targets (1s) at corners; distances in "km units" = 2
    data = np.array([
        [1, 0, 0],
        [0, 0, 0],
        [0, 0, 1],
    ], dtype=np.uint8)
    pixel_values = [1]
    dist = BandProcess.create_distance_raster(data, pixel_value=pixel_values, pixel_size_in_km=2)

    assert dist.shape == data.shape
    # Distance at a target pixel should be 0
    assert dist[0, 0] == pytest.approx(0.0)
    assert dist[2, 2] == pytest.approx(0.0)
    # Middle should be min distance to either corner times pixel_size_in_km
    # KDTree distance is in pixel grid steps; center (1,1) to (0,0) is sqrt(2) steps.
    assert dist[1, 1] == pytest.approx(np.sqrt(2) * 2.0)
