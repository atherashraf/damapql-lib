from typing import List, Dict, Iterable, Union, Optional, Any
from pathlib import Path

import fiona
import geopandas as gpd
import mercantile
import pandas as pd
from pyproj import CRS
from shapely.geometry import Polygon, box, Point, LineString, MultiLineString, MultiPolygon
from shapely.geometry import base as shapely_base

from digitalarzengine.utils.singletons import da_logger


class GPDVector(gpd.GeoDataFrame):
    """
    A light subclass of GeoDataFrame with convenience constructors,
    spatial helpers, and robust vertex extraction.
    """

    # Tell pandas/geopandas which attributes to carry over in operations
    _metadata: List[str] = []

    def __init__(self, gdf: Optional[gpd.GeoDataFrame] = None):
        if gdf is None:
            gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        elif not hasattr(gdf, "geometry"):
            g_cols = self.get_geometry_columns(gdf)
            if g_cols:
                gdf = gdf.set_geometry(g_cols[0], crs=getattr(gdf, "crs", None))

        super().__init__(gdf)

    @property
    def _constructor(self):
        # Ensure geopandas operations return our subclass
        return GPDVector

    # --------------------------
    # IO & Validation Helpers
    # --------------------------
    @staticmethod
    def layer_exists(filepath: Union[str, Path], layer_name: str) -> bool:
        """
        Checks if a specific layer exists within a vector file (e.g., GeoPackage).
        """
        try:
            return layer_name in fiona.listlayers(str(filepath))
        except (OSError, fiona.errors.DriverError):
            return False

    @classmethod
    def from_file_safe(cls, filepath: Union[str, Path], layer: str) -> Optional["GPDVector"]:
        """
        Loads a layer if it exists, otherwise returns None.
        """
        if cls.layer_exists(filepath, layer):
            return cls(gpd.read_file(filepath, layer=layer))
        return None

    # --------------------------
    # Constructors / Converters
    # --------------------------
    @staticmethod
    def from_geojson(features: List[Dict], crs: Union[CRS, str, int] = "EPSG:4326") -> "GPDVector":
        gdf = gpd.GeoDataFrame.from_features(features, crs=crs) if features else gpd.GeoDataFrame(geometry=[], crs=crs)
        return GPDVector(gdf)

    @staticmethod
    def to_geojson(gdf: gpd.GeoDataFrame) -> Dict:
        return gdf.__geo_interface__

    def get_gdf(self) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(self.copy(), crs=self.crs, geometry=self.geometry.name)

    @classmethod
    def from_csv(
            cls,
            filepath: Union[str, Path],
            lon_col: str="lon",
            lat_col: str="lat",
            crs: Union[str, int, CRS] = "EPSG:4326",
            drop_coords: bool = False,
            **kwargs
    ) -> "GPDVector":
        """
        Creates a GPDVector from a CSV file containing latitude and longitude columns.

        Parameters:
            filepath: Path to the CSV file.
            lon_col: Name of the column containing longitude / X coordinates.
            lat_col: Name of the column containing latitude / Y coordinates.
            crs: Coordinate Reference System for the geometry (e.g., 'EPSG:4326', 4326).
            drop_coords: If True, deletes the original lat/lon columns from the final dataframe.
            **kwargs: Additional arguments passed directly to pandas.read_csv (e.g., sep, encoding).
        """
        import pandas as pd

        # 1. Read the CSV file into a pandas DataFrame
        df = pd.read_csv(filepath, **kwargs)

        # 2. Drop rows where coordinates are missing (NaN)
        df = df.dropna(subset=[lon_col, lat_col])

        # 3. Create the geometry array using geopandas' optimized points_from_xy
        geometry = gpd.points_from_xy(df[lon_col], df[lat_col], crs=crs)

        # 4. Construct the GeoDataFrame
        gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=crs)

        # 5. Optionally drop the raw latitude/longitude columns
        if drop_coords:
            gdf = gdf.drop(columns=[lon_col, lat_col])

        return cls(gdf)

    @staticmethod
    def from_shapely(geom: shapely_base.BaseGeometry, crs: Union[str, int, CRS] = "EPSG:4326") -> "GPDVector":
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[geom], crs=crs)
        return GPDVector(gdf)

    @staticmethod
    def to_aoi_gdf(gdf: gpd.GeoDataFrame) -> "GPDVector":
        union_geom = gdf.geometry.unary_union
        return GPDVector.from_shapely(union_geom, crs=gdf.crs)

    # --------------------------
    # Spatial Ops
    # --------------------------
    def is_intersects(self, other: Union[gpd.GeoDataFrame, shapely_base.BaseGeometry]) -> gpd.GeoSeries:
        if isinstance(other, gpd.GeoDataFrame):
            other2 = other if str(other.crs) == str(self.crs) else other.to_crs(self.crs)
            target = other2.geometry.unary_union
        else:
            target = other
        return self.geometry.intersects(target)

    # --------------------------
    # Tiling Helpers
    # --------------------------
    @staticmethod
    def convert_tile_zxy_to_gdf(x: int, y: int, z: int) -> "GPDVector":
        b = mercantile.bounds(x, y, z)
        poly = Polygon([(b.west, b.south), (b.west, b.north), (b.east, b.north), (b.east, b.south)])
        gdf = gpd.GeoDataFrame({"x": [x], "y": [y], "z": [z]}, geometry=[poly], crs="EPSG:4326")
        return GPDVector(gdf)

    @staticmethod
    def get_zxy_tiles(aoi_gdf: gpd.GeoDataFrame, zoom: Union[int, Iterable[int]], inside_aoi: bool = True) -> "GPDVector":
        aoi_4326 = aoi_gdf.to_crs(epsg=4326)
        xmin, ymin, xmax, ymax = aoi_4326.total_bounds

        tiles = []
        for t in mercantile.tiles(xmin, ymin, xmax, ymax, zooms=zoom):
            tb = mercantile.bounds(t)
            geom = box(tb.west, tb.south, tb.east, tb.north)
            if not inside_aoi or aoi_4326.intersects(geom).any():
                tiles.append({"x": t.x, "y": t.y, "z": t.z, "geometry": geom})

        if not tiles:
            return GPDVector(gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))

        return GPDVector(gpd.GeoDataFrame(tiles, crs="EPSG:4326", geometry="geometry"))

    # --------------------------
    # Utility & Geometry Processing
    # --------------------------
    @staticmethod
    def get_geometry_columns(df) -> List[str]:
        return [c for c, dt in df.dtypes.items() if str(dt) == "geometry"]

    @classmethod
    def extent_2_envelop(cls, min_x: float, min_y: float, max_x: float, max_y: float, crs: Union[str, int, CRS]) -> "GPDVector":
        if min_x > max_x: min_x, max_x = max_x, min_x
        if min_y > max_y: min_y, max_y = max_y, min_y

        geom = box(min_x, min_y, max_x, max_y)
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[geom], crs=crs)
        return cls(gdf)

    @staticmethod
    def multipolygon_to_polygon(gdf) -> 'GPDVector':
        gdf = gdf.explode(index_parts=False)
        gdf = gdf[gdf.geometry.type == "Polygon"]
        return GPDVector(gdf)

    def extract_vertices(self, geom: shapely_base.BaseGeometry) -> List[Point]:
        """
        Generic extraction of all vertices from any geometry type into a list of Points.
        Includes exterior and interior (holes) for Polygons.
        """
        if geom is None or geom.is_empty:
            return []

        # Handle Multi-parts and Collections
        if hasattr(geom, "geoms"):
            points = []
            for part in geom.geoms:
                points.extend(self.extract_vertices(part))
            return points

        # Handle Polygons (Exterior + Interiors)
        if isinstance(geom, Polygon):
            points = [Point(pt) for pt in geom.exterior.coords]
            for ring in geom.interiors:
                points.extend([Point(pt) for pt in ring.coords])
            return points

        # Handle LineStrings and Points
        if hasattr(geom, "coords"):
            return [Point(pt) for pt in geom.coords]

        return []

    def line_vertices_to_points(self, geom: shapely_base.BaseGeometry) -> List[Point]:
        """
        Legacy wrapper for extract_vertices specifically for line-to-point workflows.
        """
        return self.extract_vertices(geom)


    def zonal_statistics(
            self,
            admin_gdf: gpd.GeoDataFrame,
            zone_id_field: str,
            value_field: str
    ) -> pd.DataFrame:
        """
        Calculates descriptive zonal statistics for point values aggregated by
        administrative boundaries or classified regions. Automatically dissolves
        multi-polygon classes if duplicates are detected.
        """
        if self.empty or admin_gdf.empty:
            raise ValueError("One of the input GeoDataFrames is empty.")

        # 1. Align Coordinate Reference Systems
        points_gdf = self
        if points_gdf.crs != admin_gdf.crs:
            points_gdf = points_gdf.to_crs(admin_gdf.crs)

        # 2. Check for duplicate classes and dissolve if necessary
        # This combines split polygons of the same class into a single MultiPolygon row
        if admin_gdf[zone_id_field].duplicated().any():
            da_logger.debug(f"Duplicate values found in '{zone_id_field}'. Dissolving multi-polygon classes...")
            # .dissolve() groups by the column and merges geometries spatially
            admin_gdf = admin_gdf.dissolve(by=zone_id_field).reset_index()

        # 3. Spatial Join: Find which consolidated class contains each point
        joined = gpd.sjoin(points_gdf, admin_gdf, how="inner", predicate="within")

        if joined.empty:
            raise ValueError("No points intersect with the provided geometries.")

        # 4. Define Safe Coefficient of Variation (std / mean)
        def cv_func(x):
            m = x.mean()
            return x.std() / m if m != 0 and not pd.isna(m) else 0.0

        # 5. Group by Class/Zone and calculate statistics
        stats_df = (
            joined.groupby(zone_id_field)[value_field]
            .agg(["min", "max", "count", "mean", "median", "std", cv_func])
            .reset_index()
        )
        stats_df = stats_df.rename(columns={"cv_func": "cv"})

        # 6. Ensure ALL classes are kept in the final table (even those with 0 points)
        admin_base_df = pd.DataFrame(admin_gdf[[zone_id_field]])
        final_df = pd.merge(admin_base_df, stats_df, on=zone_id_field, how="left")

        # 7. Prefix and clean columns
        stat_metrics = ["min", "max", "count", "mean", "median", "std", "cv"]
        final_df[stat_metrics] = final_df[stat_metrics].fillna(0.0)

        rename_mapping = {metric: f"{value_field}_{metric}" for metric in stat_metrics}
        final_df = final_df.rename(columns=rename_mapping)

        return final_df


    def plot_data(
            self,
            output_fp: Union[str, Path],
            admin_gdf: gpd.GeoDataFrame,
            title: Optional[str] = None,
            *,
            value_field: Optional[str] = None,
            cmap: str = "viridis",
            marker_size: float = 15.0,
            edgecolor: str = "black",
            linewidth: float = 0.8,
            admin_fill_color: str = "none",
            label_field: Optional[str] = None,
            label_style: Optional[Dict[str, Any]] = None,
            **kwargs
    ) -> Path:
        """
        Plots point features over administrative boundaries and saves the output map.

        Parameters:
            output_fp: Destination file path for the exported image.
            admin_gdf: GeoDataFrame containing the boundary polygons.
            title: Optional title for the generated map.
            value_field: Optional column name in points to color-code points.
            cmap: Colormap used if value_field is provided.
            marker_size: Size of the point markers.
            edgecolor: Line color for the administrative boundaries.
            linewidth: Thickness of the administrative boundaries.
            admin_fill_color: Background fill for admin polygons (defaults to transparent "none").
            label_field: Column name from admin_gdf to use for text labels.
            label_style: Dictionary overriding default matplotlib text properties.
            **kwargs: Extra arguments passed to matplotlib's savefig or points plot.
        """
        import matplotlib.pyplot as plt

        if self.empty:
            raise ValueError("Points layer is empty. Cannot plot.")

        output_fp = Path(output_fp)
        output_fp.parent.mkdir(parents=True, exist_ok=True)

        # Match projections to the administrative boundaries CRS
        points_gdf = self
        if admin_gdf.crs is not None and points_gdf.crs != admin_gdf.crs:
            points_gdf = points_gdf.to_crs(admin_gdf.crs)

        fig, ax = plt.subplots(figsize=(10, 10))

        # 1. Plot administrative boundaries base layer
        admin_gdf.plot(
            ax=ax,
            facecolor=admin_fill_color,
            edgecolor=edgecolor,
            linewidth=linewidth,
            zorder=1
        )

        # 2. Plot the point features overlay
        plot_kwargs = {"ax": ax, "markersize": marker_size, "zorder": 2}
        if value_field and value_field in points_gdf.columns:
            plot_kwargs.update({"column": value_field, "cmap": cmap, "legend": True})
            # Adjust legend appearance slightly to match class styling standard
            plot_kwargs.update({"legend_kwds": {"shrink": 0.6, "label": value_field}})
        else:
            plot_kwargs.update({"color": "red"})  # Default fallback point color

        points_gdf.plot(**plot_kwargs)

        # 3. Apply Admin Boundaries Text Labels
        if label_field and label_field in admin_gdf.columns:
            style = {
                "fontsize": 8,
                "color": edgecolor if edgecolor != "none" else "black",
                "ha": "center",
                "va": "center",
                "weight": "bold"
            }
            if label_style:
                style.update(label_style)

            for _, row in admin_gdf.iterrows():
                if row.geometry is None or row.geometry.is_empty:
                    continue
                # Safely find a point inside the polygon for labeling
                p = row.geometry.representative_point()
                ax.text(p.x, p.y, str(row[label_field]), **style)

        if title:
            ax.set_title(title, fontsize=14, pad=10)

        ax.set_axis_off()
        plt.tight_layout()
        fig.savefig(output_fp, dpi=300, bbox_inches="tight", pad_inches=0.05, **kwargs)
        plt.close(fig)

        return output_fp

    def polygon_to_polyline(self) -> "GPDVector":
        """
        Converts Polygon or MultiPolygon geometries into LineStrings.

        Extracts both the outer boundaries (exteriors) and any internal
        holes (interiors) into explicit line components while preserving
        all non-spatial attribute data columns.

        Returns:
            GPDVector: A new instance containing LineString features.
        """
        if self.empty:
            return GPDVector(gpd.GeoDataFrame(geometry=[], crs=self.crs))

        lines_records = []
        geom_col_name = self.geometry.name

        # Iterate over each row to extract lines while retaining attributes
        for idx, row in self.iterrows():
            geom = row[geom_col_name]
            if geom is None or geom.is_empty:
                continue

            # Standardize geometry structures into a iterable sequence
            if isinstance(geom, MultiPolygon):
                polygons = list(geom.geoms)
            elif isinstance(geom, Polygon):
                polygons = [geom]
            else:
                # If the feature is already a line or point, pass it as-is or skip
                da_logger.warning(f"Row index {idx} ignored: expected Polygon, got {geom.geom_type}")
                continue

            # Create a base dictionary template of all data columns for this row
            base_record = row.drop(geom_col_name).to_dict()

            for poly in polygons:
                # 1. Extract the primary outer edge boundary
                if poly.exterior and len(poly.exterior.coords) >= 2:
                    ext_record = base_record.copy()
                    ext_record[geom_col_name] = LineString(poly.exterior.coords)
                    lines_records.append(ext_record)

                # 2. Extract any interior hole boundaries
                for interior in poly.interiors:
                    if len(interior.coords) >= 2:
                        int_record = base_record.copy()
                        int_record[geom_col_name] = LineString(interior.coords)
                        lines_records.append(int_record)

        # Re-pack the derived record structures back into our GPDVector class
        if not lines_records:
            return GPDVector(gpd.GeoDataFrame(geometry=[], crs=self.crs))

        new_gdf = gpd.GeoDataFrame(lines_records, crs=self.crs).set_geometry(geom_col_name)
        return GPDVector(new_gdf)