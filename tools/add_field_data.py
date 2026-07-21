import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import rowcol

WINDOW_NAME = "Field Data Editor"
POINT_RADIUS = 6
HIT_RADIUS = 12
PANEL_HEIGHT = 70


@dataclass
class FieldAppState:
    image_bgr: np.ndarray
    gdf: gpd.GeoDataFrame
    field_name: str
    label_field: str
    output_path: Path
    raster_transform: rasterio.transform.Affine
    raster_crs: rasterio.crs.CRS
    point_color: tuple[int, int, int]
    label_color: tuple[int, int, int]
    selected_idx: int | None = None
    dragging: bool = False
    add_mode: bool = False  # True when creating an entirely new point
    edit_mode: bool = False  # True when modifying an attribute value via console
    show_labels: bool = False

    @property
    def image_height(self) -> int:
        return self.image_bgr.shape[0]

    @property
    def image_width(self) -> int:
        return self.image_bgr.shape[1]


def nearest_point(gdf: gpd.GeoDataFrame, x: int, y: int) -> int | None:
    if gdf.empty:
        return None

    dx = gdf["_pixel_j"].to_numpy(dtype=float) - x
    dy = gdf["_pixel_i"].to_numpy(dtype=float) - y
    distances = np.sqrt(dx * dx + dy * dy)

    idx = int(np.argmin(distances))
    return idx if distances[idx] <= HIT_RADIUS else None


def is_null_or_empty(val) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and np.isnan(val):
        return True
    if pd.isna(val):
        return True
    if str(val).strip().lower() in ["nan", "none", "", "<na>"]:
        return True
    return False


def draw_ui(state: FieldAppState):
    canvas = np.full(
        (state.image_bgr.shape[0] + PANEL_HEIGHT, state.image_bgr.shape[1], 3),
        240,
        dtype=np.uint8,
    )
    canvas[: state.image_bgr.shape[0], : state.image_bgr.shape[1]] = state.image_bgr.copy()

    for pos_idx in range(len(state.gdf)):
        row = state.gdf.iloc[pos_idx]
        x = int(round(row["_pixel_j"]))
        y = int(round(row["_pixel_i"]))

        label_val = row[state.label_field]
        color = (255, 255, 0) if pos_idx == state.selected_idx else state.point_color

        cv2.circle(canvas, (x, y), POINT_RADIUS, color, -1)
        cv2.circle(canvas, (x, y), POINT_RADIUS + 2, (255, 255, 255), 1)

        if state.show_labels and not is_null_or_empty(label_val):
            display_text = str(label_val)
            cv2.putText(canvas, display_text, (x + 9, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(canvas, display_text, (x + 9, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, state.label_color, 1,
                        cv2.LINE_AA)

    # UI Information Status Bar Display Setup
    status_label_str = "ON" if state.show_labels else "OFF"
    help_text = f"Drag points | A: Add Pt | F: Edit Val | L: Labels ({status_label_str}) | S: Save | Q/Esc: Quit"

    if state.add_mode:
        cv2.putText(canvas, "ADD POINT MODE ACTIVE - Click anywhere on map image to place a new point",
                    (20, state.image_bgr.shape[0] + 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1, cv2.LINE_AA)
    elif state.edit_mode:
        cv2.putText(canvas, f"FIELD EDITOR ACTIVE - Click an existing point to edit '{state.field_name}'",
                    (20, state.image_bgr.shape[0] + 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)

    cv2.putText(canvas, help_text, (20, state.image_bgr.shape[0] + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (40, 40, 40), 1,
                cv2.LINE_AA)
    cv2.imshow(WINDOW_NAME, canvas)


def add_new_point_geometry(state: FieldAppState, x: int, y: int):
    """Inserts a new spatial point into the GeoDataFrame and immediately requests its field value."""
    print(f"\n--- Adding New Point at pixel ({x}, {y}) ---")
    val = input(f"Enter value for '{state.field_name}': ")

    # Create blank row data frame dictionary
    new_row = {col: np.nan for col in state.gdf.columns}
    new_row["_pixel_j"] = x
    new_row["_pixel_i"] = y
    new_row[state.field_name] = val

    # Generate geographic fallback geometry temporarily (will be finalized on save)
    from shapely.geometry import Point
    new_row["geometry"] = Point(0, 0)

    # Append securely and reindex
    state.gdf.loc[len(state.gdf)] = new_row
    state.gdf = state.gdf.reset_index(drop=True)
    state.selected_idx = len(state.gdf) - 1
    print("New point vector stored successfully.")


def mouse_callback(event, x, y, flags, param):
    state: FieldAppState = param

    if y >= state.image_bgr.shape[0]:
        return

    if event == cv2.EVENT_LBUTTONDOWN:
        # 1. ADD NEW POINT ACTIVE
        if state.add_mode:
            state.add_mode = False
            add_new_point_geometry(state, x, y)
            return

        idx = nearest_point(state.gdf, x, y)
        if idx is not None:
            state.selected_idx = idx

            # 2. EDIT FIELD VALUE ACTIVE
            if state.edit_mode:
                state.edit_mode = False
                current_val = state.gdf.iloc[idx][state.field_name]
                print(f"\n--- Editing Existing Point #{idx + 1} ---")
                print(f"Current Value: {current_val}")
                new_val = input(f"Enter new value for '{state.field_name}': ")

                val_col_idx = state.gdf.columns.get_loc(state.field_name)
                state.gdf.iat[idx, val_col_idx] = new_val
                print(f"Updated Point attribute successfully.")
                return

            state.dragging = True

    elif event == cv2.EVENT_MOUSEMOVE:
        if state.dragging and state.selected_idx is not None:
            j_col_idx = state.gdf.columns.get_loc("_pixel_j")
            i_col_idx = state.gdf.columns.get_loc("_pixel_i")
            state.gdf.iat[state.selected_idx, j_col_idx] = x
            state.gdf.iat[state.selected_idx, i_col_idx] = y

    elif event == cv2.EVENT_LBUTTONUP:
        state.dragging = False


def save_output_file(state: FieldAppState):
    print("Recalculating geographic spatial geometries...")
    new_geometries = []
    for idx in range(len(state.gdf)):
        row = state.gdf.iloc[idx]
        geo_x, geo_y = rasterio.transform.xy(state.raster_transform, row["_pixel_i"], row["_pixel_j"])
        from shapely.geometry import Point
        new_geometries.append(Point(geo_x, geo_y))

    export_gdf = state.gdf.copy()
    export_gdf["geometry"] = new_geometries
    export_gdf = export_gdf.drop(columns=["_pixel_j", "_pixel_i"])

    state.output_path.parent.mkdir(parents=True, exist_ok=True)
    ext = state.output_path.suffix.lower()
    if ext == ".gpkg":
        export_gdf.to_file(state.output_path, driver="GPKG")
    else:
        export_gdf.to_file(state.output_path)
    print(f"Saved update layers successfully to: {state.output_path}")


def add_field_data(raster_fp: str | Path,
                   gdf_input: gpd.GeoDataFrame,
                   field_name: str,
                   label_field: str,
                   output_fp: str | Path,
                   color: tuple[int, int, int] = (0, 0, 255),
                   label_color: tuple[int, int, int] = (255, 255, 255)):
    raster_path = Path(raster_fp)
    output_path = Path(output_fp)

    with rasterio.open(raster_path) as src:
        transform = src.transform
        crs = src.crs
        if src.count >= 3:
            img = np.dstack((src.read(3), src.read(2), src.read(1)))
        else:
            img = src.read(1)
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        if img.dtype != np.uint8:
            img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    gdf = gdf_input.copy()
    if gdf.crs != crs:
        print(f"Reprojecting Vector Layer from {gdf.crs} to match Raster Alignment CRS: {crs}")
        gdf = gdf.to_crs(crs)

    if field_name not in gdf.columns:
        print(f"Data column field '{field_name}' not found. Initializing with NaN values.")
        gdf[field_name] = np.nan
    else:
        gdf[field_name] = gdf[field_name].astype(object)

    if label_field not in gdf.columns:
        print(f"Label sourcing column '{label_field}' not found. Creating empty proxy column layout.")
        gdf[label_field] = np.nan

    pixel_js = []
    pixel_is = []
    for geom in gdf.geometry:
        if geom.is_empty:
            pixel_js.append(0)
            pixel_is.append(0)
        else:
            r, c = rowcol(transform, geom.x, geom.y)
            pixel_js.append(c)
            pixel_is.append(r)

    gdf["_pixel_j"] = pixel_js
    gdf["_pixel_i"] = pixel_is

    state = FieldAppState(
        image_bgr=img,
        gdf=gdf,
        field_name=field_name,
        label_field=label_field,
        output_path=output_path,
        raster_transform=transform,
        raster_crs=crs,
        point_color=color,
        label_color=label_color
    )

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, mouse_callback, state)

    print("\n--- Field Data Editing Tool Terminal Logs ---")
    print("Instructions:")
    print("  Click & Drag Points : Move point spatial placement context locations")
    print("  A                   : Arm Add Point Mode -> Click anywhere to add a fresh point node")
    print("  F                   : Arm Field Editor Mode -> Click an existing point to set its field value")
    print("  L                   : Toggle visibility of non-null point labels text attributes")
    print("  S                   : Save geometric shapes adjustments and modifications to file disk")
    print("  Q / Esc             : Terminate framework process thread instance")

    while True:
        draw_ui(state)
        key = cv2.waitKey(30) & 0xFF

        if key == ord("a"):
            state.add_mode = True
            state.edit_mode = False  # Clear opposite mode
            print("Add Point Mode armed. Left-click anywhere on the map image.")

        elif key == ord("f"):
            state.edit_mode = True
            state.add_mode = False  # Clear opposite mode
            print(f"Field Editor armed. Click an existing point marker to edit its '{state.field_name}' value.")

        elif key == ord("l"):
            state.show_labels = not state.show_labels
            print(f"Labels Display toggled to: {state.show_labels}")

        elif key == ord("s"):
            save_output_file(state)

        elif key == ord("q") or key == 27:
            print("Session terminated.")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    from settings import INPUT_DIR

    test_raster = INPUT_DIR / "georeferencing/processed/geology.tif"
    test_shapefile_input = INPUT_DIR / "georeferencing/raw/sample_points.shp"
    test_output = INPUT_DIR / "georeferencing/processed/updated_field_points.shp"

    if test_raster.exists() and test_shapefile_input.exists():
        input_gdf = gpd.read_file(test_shapefile_input)

        add_field_data(
            raster_fp=test_raster,
            gdf_input=input_gdf,
            field_name="WaterLevel",
            label_field="WaterLevel",
            output_fp=test_output,
            color=(0, 0, 255),  # Red default markers
            label_color=(255, 255, 255)  # White labels text
        )