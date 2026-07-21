import math

from build.lib.digitalarzengine.utils.singletons import da_logger
from digitalarzengine.utils.styling.raster_styling import DiscreteStyle, ContinuousStyle


def hex_to_rgba(hex_color: str):
    h = hex_color.lstrip('#')
    if len(h) == 6:
        h += "FF"
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))


def test_continuous_style_colors():
    style_info = {
        "min": 0,
        "max": 1,
        "palette" : ["#0000FF", "#00FF00", "#FF0000"],
    }
    style = ContinuousStyle(**style_info)
    cm = style.to_cog_color_map()
    da_logger.debug(f"continuous style {cm}")
    # Expect two equal-width bins + overflow
    # step = (100 - 0) / (3 - 1) = 50
    assert len(cm) == len(style_info["palette"])

    # First bin: [0, 50) uses first color
    (r0, c0) = cm[0]
    assert r0 == (0.0, style_info["max"]/(len(style_info["palette"]) -1))
    assert c0 == hex_to_rgba("#0000FF")

    # Second bin: [50, 100) uses second color
    (r1, c1) = cm[1]
    assert r1 == (style_info["max"]/(len(style_info["palette"]) -1), style_info["max"])
    assert c1 == hex_to_rgba("#00FF00")

    # Overflow bin: [100, +inf) uses last color
    (ro, co) = cm[2]
    assert ro[0] == style_info["max"] and math.isinf(ro[1]) and ro[1] > 0
    assert co == hex_to_rgba("#FF0000")


def test_crop_discrete_raster_style():
    values = list(range(13))  # 0..12
    style_info = {
        "labels": ["", "Rice", "Summer_Maize", "Cotton", "Sesame", "Fodder", "Orchards", "Sugarcane", "Others",
                   "Trees and Plantation", "Builtup Area", "Fallow Land", "Water Bodies"],
        "palette": {"0": "#00000000", "1": "#00ff00", "2": "#ff00ff", "3": "#ccb380", "4": "#ffb3b3",
                    "5": "#ffff00", "6": "#008000", "7": "#804dff", "8": "#ff8000", "9": "#808000",
                    "10": "#ff0000", "11": "#804d33", "12": "#0000ff"}}

    style: DiscreteStyle = DiscreteStyle(**style_info)
    cm = style.to_cog_color_map()
    da_logger.debug(f"crop cm {cm}")

    labels = style.get_labels()
    da_logger.debug(f"crop labels {labels}")
    # Should produce one bin per adjacent pair + one overflow bin
    assert len(cm) == len(values), "Expected N bins for N breakpoints including overflow."
    assert len(labels) == len(values), "Expected N bins for N breakpoints including overflow."
    # Check first bin: [0, 1) uses palette index 0
    first_range, first_rgba = cm[0]
    assert first_range == (0.0, 1.0)
    assert first_rgba == hex_to_rgba(style_info["palette"]["0"])

    # Check overflow bin: [12, +inf) uses palette index 12
    last_range, last_rgba = cm[-1]
    assert last_range[0] == float(values[-1])
    assert math.isinf(last_range[1]) and last_range[1] > 0
    assert last_rgba == hex_to_rgba(style_info["palette"]["12"])


def test_gw_discrete_raster_style():
    style_info = {"values": [8.028, 22.612, 36.073, 40.5, 44.586, 49.211, 53.479, 67.529],
                  "max_val": 80.4974365234375,
                  "min_val": 8.028019905090332,
                  "palette": ["#40bf4fe8", "#419b6fe8", "#43768fe8", "#4452afe8", "#5640ade8", "#79408ae9", "#9c4067e9",
                              "#bf4044ea"]}

    style: DiscreteStyle = DiscreteStyle(**style_info)
    cm = style.to_cog_color_map()
    da_logger.debug(f"gw cm: {cm}")

    labels = style.get_labels()
    da_logger.debug(f"gw labels {labels}")

    values = style_info["values"]
    # Should produce one bin per adjacent pair + one overflow bin
    assert len(cm) == len(values), "Expected N bins for N breakpoints including overflow."
    assert len(labels) == len(values), "Expected N bins for N breakpoints including overflow."

    first_range, first_rgba = cm[0]
    assert first_range == (values[0], values[1])
    assert first_rgba == hex_to_rgba(style_info["palette"][0])

    # Check overflow bin: [12, +inf) uses palette index 12
    last_range, last_rgba = cm[-1]
    assert last_range[0] == float(values[-1])
    assert math.isinf(last_range[1]) and last_range[1] > 0
    assert last_rgba == hex_to_rgba(style_info["palette"][-1])


def test_soil_discrete_raster_style():
    style_info = {"labels": ["highly acidic", "acidic", "neutral", "alkaline", "highly alkaline"],
                  "values": [5.5, 6.5, 7.7, 8.9, 9.9],
                  "max_val": 9.5, "min_val": 7.4,
                  "palette": ["#bb232a", "#713154", "#1a9641", "#fff786", "#d7191c"]}

    style: DiscreteStyle = DiscreteStyle(**style_info)
    cm = style.to_cog_color_map()
    da_logger.debug(f"soil cm: {cm}")

    labels = style.get_labels()
    da_logger.debug(f"soil labels {labels}")

    values = style_info["values"]
    # Should produce one bin per adjacent pair + one overflow bin
    assert len(cm) == len(values), "Expected N bins for N breakpoints including overflow."
    assert len(labels) == len(values), "Expected N bins for N breakpoints including overflow."

    first_range, first_rgba = cm[0]
    assert first_range == (values[0], values[1])
    assert first_rgba == hex_to_rgba(style_info["palette"][0])

    # Check overflow bin: [12, +inf) uses palette index 12
    last_range, last_rgba = cm[-1]
    assert last_range[0] == float(values[-1])
    assert math.isinf(last_range[1]) and last_range[1] > 0
    assert last_rgba == hex_to_rgba(style_info["palette"][-1])
