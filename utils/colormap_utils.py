from __future__ import annotations

from typing import Optional, Tuple, Union

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import (
    Colormap,
    ListedColormap,
    BoundaryNorm,
    LinearSegmentedColormap,
    Normalize,
)


class ColorMapUtils:
    """
    Utilities for building and transforming Matplotlib colormaps.

    This class is designed to be framework-agnostic and reusable across your
    plotting pipeline (e.g., RioProcess collage maps, Folium overlays, reports).

    Main capabilities
    -----------------
    1) `cmap_growth(...)`:
       Create a new colormap by *re-sampling* an existing colormap with a
       nonlinear curve ("exp" or "log") to visually emphasize high values
       (useful for arid NDVI where low values should look closer to white).

    2) `colormap_from_minmax_df(...)`:
       Build a discrete or continuous color ramp from a DataFrame describing
       intervals and colors: [min, max, color].

    Notes
    -----
    - These utilities do not modify raster values; they only affect color lookup.
      For mathematically correct nonlinear mapping of data values, use Matplotlib
      `norm` objects (e.g., `PowerNorm`, `TwoSlopeNorm`). Here, we intentionally
      avoid requiring `norm` by baking the mapping into the colormap.
    """

    @staticmethod
    def cmap_growth(
        cmap: Union[str, Colormap],
        *,
        mode: str = "exp",
        gamma: float = 2.0,
        alpha: float = 10.0,
        n: int = 256,
        name: Optional[str] = None,
    ) -> LinearSegmentedColormap:
        """
        Create a "grown" version of any Matplotlib colormap using a nonlinear
        re-sampling curve.

        This is a generic utility: you can pass any colormap name ("Greens",
        "viridis", "RdYlGn") or an existing Colormap instance.

        Parameters
        ----------
        cmap : str | matplotlib.colors.Colormap
            Base colormap to transform.
            - If str: must be a valid Matplotlib colormap name.
            - If Colormap: used directly.
        mode : {"exp", "log"}, default="exp"
            Growth mode:
            - "exp": exponential / power-law curve: x' = x**gamma
            - "log": logarithmic curve: x' = log1p(alpha*x) / log1p(alpha)
        gamma : float, default=2.0
            Exponential parameter used only when mode="exp".
            - gamma > 1: compresses low values (more white/light dominance)
            - gamma = 1: identity (no change)
            - gamma < 1: expands low values
        alpha : float, default=10.0
            Log parameter used only when mode="log".
            Larger alpha increases compression near 0 (more whitening at low end).
        n : int, default=256
            Number of samples used to build the new colormap.
            Higher values can make smoother gradients.
        name : str | None, optional
            Name of the returned colormap. If None, a name is auto-generated.

        Returns
        -------
        matplotlib.colors.LinearSegmentedColormap
            A new colormap instance representing the nonlinear "grown" variant.

        Raises
        ------
        ValueError
            If mode is not {"exp", "log"}, or gamma/alpha are invalid.

        Examples
        --------
        # Arid NDVI: make low values near-white while keeping high values dark
        cmap2 = ColorMapUtils.cmap_growth("Greens", mode="exp", gamma=2.2)

        # More aggressive whitening using log
        cmap3 = ColorMapUtils.cmap_growth("Greens", mode="log", alpha=12)

        # Use with your collage (no norm required)
        # RioProcess.create_collage_images(..., cmap=cmap2, vmin=0, vmax=1)
        """
        base = plt.get_cmap(cmap) if isinstance(cmap, str) else cmap

        x = np.linspace(0, 1, n)
        mode = mode.lower().strip()

        if mode == "exp":
            if gamma <= 0:
                raise ValueError("gamma must be > 0 for exp mode.")
            xg = x ** gamma

        elif mode == "log":
            if alpha <= 0:
                raise ValueError("alpha must be > 0 for log mode.")
            xg = np.log1p(alpha * x) / np.log1p(alpha)

        else:
            raise ValueError("mode must be 'exp' or 'log'.")

        cols = base(xg)
        new_name = name or f"{base.name}_{mode}"
        return LinearSegmentedColormap.from_list(new_name, cols, N=n)

    @staticmethod
    def colormap_from_minmax_df(
        df: pd.DataFrame,
        *,
        vmin_col: str = "min",
        vmax_col: str = "max",
        color_col: str = "color",
        mode: str = "discrete",
        clip: bool = True,
        name: str = "custom_ramp",
        nan_color: str = "#ffffff",
    ) -> Tuple[Union[ListedColormap, LinearSegmentedColormap], Union[BoundaryNorm, Normalize], np.ndarray]:
        """
        Build a Matplotlib color ramp from a DataFrame describing value ranges.

        The DataFrame must define intervals and corresponding colors:
            [min, max, color]

        Parameters
        ----------
        df : pandas.DataFrame
            Ramp definition table. Must contain columns specified by `vmin_col`,
            `vmax_col`, and `color_col`.
        vmin_col : str, default="min"
            Column name holding interval lower bounds.
        vmax_col : str, default="max"
            Column name holding interval upper bounds.
        color_col : str, default="color"
            Column name holding colors (hex strings, Matplotlib names, RGB tuples).
        mode : {"discrete", "continuous"}, default="discrete"
            - "discrete": one color per interval bin (ListedColormap + BoundaryNorm)
            - "continuous": smooth interpolation across edges (LinearSegmentedColormap + Normalize)
        clip : bool, default=True
            Whether to clip values outside bounds to the nearest end color.
        name : str, default="custom_ramp"
            Name of the generated colormap.
        nan_color : str, default="#ffffff"
            Color used for NaN / nodata values (via `cmap.set_bad(...)`).

        Returns
        -------
        (cmap, norm, bounds) : tuple
            cmap : ListedColormap or LinearSegmentedColormap
                Generated colormap.
            norm : BoundaryNorm or Normalize
                Normalization mapping values -> [0, 1] for the colormap.
            bounds : numpy.ndarray
                Sorted unique edges derived from all min/max values.

        Raises
        ------
        ValueError
            If df is empty/missing columns or has invalid intervals.

        Examples
        --------
        ndvi_ramp = pd.DataFrame({
            "min":   [-1.0,  0.0, 0.2, 0.4],
            "max":   [ 0.0,  0.2, 0.4, 1.0],
            "color": ["#9ecae1", "#f7f1d5", "#bfe7bf", "#006400"]
        })

        cmap, norm, bounds = ColorMapUtils.colormap_from_minmax_df(ndvi_ramp, mode="discrete")
        # ax.imshow(band, cmap=cmap, norm=norm)
        """
        if df is None or df.empty:
            raise ValueError("df is empty; cannot create color ramp.")

        required = {vmin_col, vmax_col, color_col}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"df missing columns: {missing}. Found columns: {list(df.columns)}")

        d = df[[vmin_col, vmax_col, color_col]].copy()
        d[vmin_col] = pd.to_numeric(d[vmin_col], errors="raise").astype(float)
        d[vmax_col] = pd.to_numeric(d[vmax_col], errors="raise").astype(float)

        if (d[vmax_col] <= d[vmin_col]).any():
            bad = d.loc[d[vmax_col] <= d[vmin_col], [vmin_col, vmax_col]]
            raise ValueError(f"Found rows where max <= min:\n{bad}")

        d = d.sort_values([vmin_col, vmax_col]).reset_index(drop=True)

        edges = np.unique(np.r_[d[vmin_col].to_numpy(), d[vmax_col].to_numpy()])
        edges = np.sort(edges)

        if edges.size < 2:
            raise ValueError("Not enough unique edges to build a ramp.")

        global_min = float(edges[0])
        global_max = float(edges[-1])

        mode = mode.lower().strip()
        if mode not in {"discrete", "continuous"}:
            raise ValueError("mode must be either 'discrete' or 'continuous'.")

        if mode == "discrete":
            mids = (edges[:-1] + edges[1:]) / 2.0
            colors_for_bins = []

            for m in mids:
                row = d[(d[vmin_col] <= m) & (m < d[vmax_col])]
                if row.empty:
                    dist = np.minimum(np.abs(m - d[vmin_col]), np.abs(m - d[vmax_col]))
                    row = d.iloc[[int(np.argmin(dist.to_numpy()))]]
                colors_for_bins.append(row[color_col].iloc[0])

            cmap_out = ListedColormap(colors_for_bins, name=name)
            cmap_out.set_bad(nan_color)
            norm_out = BoundaryNorm(edges, ncolors=cmap_out.N, clip=clip)
            return cmap_out, norm_out, edges

        # continuous mode
        stops = []
        for e in edges:
            row = d[(d[vmin_col] <= e) & (e < d[vmax_col])]
            if row.empty and e == edges[-1]:
                row = d[(d[vmin_col] < e) & (e <= d[vmax_col])]
            if row.empty:
                dist = np.minimum(np.abs(e - d[vmin_col]), np.abs(e - d[vmax_col]))
                row = d.iloc[[int(np.argmin(dist.to_numpy()))]]
            stops.append(row[color_col].iloc[0])

        xs = (edges - global_min) / (global_max - global_min)
        seg = list(zip(xs.tolist(), stops))

        cmap_out = LinearSegmentedColormap.from_list(name, seg, N=256)
        cmap_out.set_bad(nan_color)
        norm_out = Normalize(vmin=global_min, vmax=global_max, clip=clip)

        return cmap_out, norm_out, edges


    @staticmethod
    def from_list(
        name: str,
        colors: list[Union[str, tuple]],
        *,
        positions: Optional[list[float]] = None,
        n: int = 256,
        nan_color: str = "#ffffff",
    ) -> LinearSegmentedColormap:
        """
        Create a LinearSegmentedColormap from an ordered list of colors.

        This is a lightweight wrapper around Matplotlib's from_list, designed
        for quick, readable colormap definitions (e.g., QA maps, masks, flags).

        Parameters
        ----------
        name : str
            Name of the colormap.
        colors : list
            List of colors (hex strings, Matplotlib names, or RGB tuples).
        positions : list[float] | None, optional
            Optional list of positions in [0, 1] corresponding to each color.
            If None, colors are evenly spaced.
        n : int, default=256
            Number of discrete samples in the colormap.
        nan_color : str, default="#ffffff"
            Color used for NaN / nodata values.

        Returns
        -------
        matplotlib.colors.LinearSegmentedColormap

        Examples
        --------
        # QA coverage: red → yellow → green
        qa_cmap = ColorMapUtils.from_list(
            "qa_coverage",
            ["#8b0000", "#ffcc00", "#006400"],
            positions=[0.0, 0.7, 1.0]
        )

        # Simple white → green
        ndvi_cmap = ColorMapUtils.from_list(
            "ndvi_white_green",
            ["#ffffff", "#006400"]
        )
        """
        if positions is not None:
            if len(colors) != len(positions):
                raise ValueError("colors and positions must have the same length.")
            if positions[0] != 0.0 or positions[-1] != 1.0:
                raise ValueError("positions must start at 0.0 and end at 1.0.")
            if any(p < 0 or p > 1 for p in positions):
                raise ValueError("positions must be within [0, 1].")

            seg = list(zip(positions, colors))
            cmap = LinearSegmentedColormap.from_list(name, seg, N=n)
        else:
            cmap = LinearSegmentedColormap.from_list(name, colors, N=n)

        cmap.set_bad(nan_color)
        return cmap
