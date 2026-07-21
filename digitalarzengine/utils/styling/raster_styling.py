from typing import Any, Mapping, Sequence, Union, Literal, Optional, Dict, List, Tuple
from pydantic import BaseModel

# Standardize RGBA as a simple tuple
RGBA = Tuple[int, int, int, int]

# We replace rio_tiler.colormap.cmap with a standard list of intervals
# Format: [((start_val, end_val), (R, G, B, A)), ...]
ColorIntervals = List[Tuple[Tuple[float, float], RGBA]]

StyleLike = Union[Mapping[str, Any], Any]


class ColorMapBase(BaseModel):
    @staticmethod
    def create_color_map(palette) -> Dict[int, RGBA]:
        if isinstance(palette, dict):
            # Sort keys to ensure consistent binning
            pairs = sorted([(int(k), v) for k, v in palette.items()], key=lambda x: x[0])
            iterable = pairs
        elif isinstance(palette, (list, tuple)):
            iterable = list(enumerate(palette))
        else:
            raise TypeError("palette must be a dict, list, or tuple")

        return {idx: ColorMapBase.hex_to_rgba(hex_color) for idx, hex_color in iterable}

    @staticmethod
    def hex_to_rgba(hex_color: str) -> RGBA:
        h = hex_color.lstrip('#')
        if len(h) == 6:
            h += "FF"
        if len(h) != 8:
            raise ValueError(f"Invalid hex color: {hex_color}")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))

    @staticmethod
    def infer_style_type(style: StyleLike) -> Literal["discrete", "continuous"]:
        def _get(obj, key):
            return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)

        t = _get(style, "type")
        if t in ("discrete", "continuous"):
            return t

        if _get(style, "values") is not None:
            return "discrete"

        if _get(style, "min") is not None and _get(style, "max") is not None:
            return "continuous"

        return "discrete"  # Default fallback


class ContinuousStyle(ColorMapBase):
    type: Optional[Literal["continuous"]] = "continuous"
    min: float
    max: float
    palette: List[str]

    def to_intervals(self) -> ColorIntervals:
        """
        Replaces to_cog_color_map. Returns list of ((start, end), RGBA)
        """
        result = []
        color_map = self.create_color_map(self.palette)
        vmin, vmax = float(self.min), float(self.max)

        if vmax <= vmin:
            raise ValueError("max must be greater than min")

        n = len(color_map)
        sorted_keys = sorted(color_map.keys())

        if n == 1:
            color = color_map[sorted_keys[0]]
            return [((vmin, vmax), color), ((vmax, float('inf')), color)]

        step = (vmax - vmin) / (n - 1)
        for i in range(n - 1):
            start = vmin + (i * step)
            end = vmin + ((i + 1) * step)
            result.append(((start, end), color_map[sorted_keys[i]]))

        # Add the overflow/max bin
        result.append(((vmax, float('inf')), color_map[sorted_keys[-1]]))
        return result


class DiscreteStyle(ColorMapBase):
    type: Optional[Literal["discrete"]] = "discrete"
    values: Optional[List[float]] = None
    palette: Union[Dict[int, str], List[str]]

    def to_intervals(self) -> ColorIntervals:
        result = []
        color_map = self.create_color_map(self.palette)

        # If no explicit values provided, use indices 0, 1, 2...
        if self.values:
            vals = [float(v) for v in self.values]
        else:
            vals = [float(k) for k in sorted(color_map.keys())]

        if len(vals) < 2:
            # Handle single value discrete map
            color = color_map[min(color_map.keys())]
            return [((vals[0], float('inf')), color)]

        for i in range(len(vals) - 1):
            start, end = vals[i], vals[i + 1]
            # Use the color corresponding to this index, or fallback to last
            color = color_map.get(i, color_map[max(color_map.keys())])
            result.append(((start, end), color))

        # Final overflow bin
        last_color = color_map.get(len(vals) - 1, color_map[max(color_map.keys())])
        result.append(((vals[-1], float('inf')), last_color))
        return result