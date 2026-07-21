import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import Affine
from scipy.linalg import lstsq

from settings import INPUT_DIR

WINDOW_NAME = "Georeferencing Tool"
POINT_RADIUS = 7
HIT_RADIUS = 12
PANEL_HEIGHT = 70


@dataclass
class AppState:
    image_bgr: np.ndarray
    csv_path: Path
    output_path: Path
    points: pd.DataFrame
    selected_idx: int | None = None
    dragging: bool = False
    georeference_requested: bool = False
    add_mode: bool = False

    @property
    def image_height(self) -> int:
        return self.image_bgr.shape[0]

    @property
    def image_width(self) -> int:
        return self.image_bgr.shape[1]


def load_points(csv_path: Path) -> pd.DataFrame:
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        required = {"j", "i", "X", "Y"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"CSV is missing columns: {sorted(missing)}")
        return df[["j", "i", "X", "Y"]].copy()

    return pd.DataFrame(columns=["j", "i", "X", "Y"])


def save_points(df: pd.DataFrame, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    print(f"Saved control points to: {csv_path}")


def compute_transformation(df: pd.DataFrame):
    if len(df) < 3:
        raise ValueError("At least 3 control points are required.")

    pixel_coords = np.array([[row["j"], row["i"], 1.0] for _, row in df.iterrows()])
    geo_coords_x = df["X"].to_numpy(dtype=float)
    geo_coords_y = df["Y"].to_numpy(dtype=float)

    trans_x, _, _, _ = lstsq(pixel_coords, geo_coords_x)
    trans_y, _, _, _ = lstsq(pixel_coords, geo_coords_y)

    return trans_x[0], trans_x[1], trans_x[2], trans_y[0], trans_y[1], trans_y[2]


def create_georeferenced_raster(df, raster_size, output_path: Path, image_data):
    width, height = raster_size
    a, b, c, d, e, f = compute_transformation(df)

    transform = Affine(a, b, c, d, e, f)

    bands = image_data.shape[2] if image_data.ndim > 2 else 1
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # OVERRIDE WINDOWS ENVIRONMENT VARIABLE CONFLICTS
    # =========================================================================
    import os

    # Get the isolated data directory that rasterio actually ships with
    rasterio_proj_dir = os.path.join(os.path.dirname(rasterio.__file__), "proj_data")

    # Force both possible naming conventions to target the fresh folder
    os.environ["PROJ_DATA"] = rasterio_proj_dir
    os.environ["PROJ_LIB"] = rasterio_proj_dir
    # =========================================================================

    with rasterio.open(
            output_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=bands,
            dtype=image_data.dtype.name,
            crs="EPSG:4326",
            transform=transform,
    ) as dst:
        if bands == 1:
            dst.write(image_data, 1)
        else:
            for band in range(bands):
                dst.write(image_data[:, :, band], band + 1)

def nearest_point(points: pd.DataFrame, x: int, y: int) -> int | None:
    if points.empty:
        return None

    dx = points["j"].to_numpy(dtype=float) - x
    dy = points["i"].to_numpy(dtype=float) - y
    distances = np.sqrt(dx * dx + dy * dy)

    idx = int(np.argmin(distances))
    return idx if distances[idx] <= HIT_RADIUS else None


def button_rect(state: AppState):
    y1 = state.image_height + 15
    y2 = state.image_height + 55
    return 15, y1, 210, y2


def is_inside_button(state: AppState, x: int, y: int) -> bool:
    x1, y1, x2, y2 = button_rect(state)
    return x1 <= x <= x2 and y1 <= y <= y2


def draw_ui(state: AppState):
    canvas = np.full(
        (state.image_height + PANEL_HEIGHT, state.image_width, 3),
        245,
        dtype=np.uint8,
    )

    canvas[: state.image_height, : state.image_width] = state.image_bgr.copy()

    for pos_idx in range(len(state.points)):
        row = state.points.iloc[pos_idx]
        x = int(round(row["j"]))
        y = int(round(row["i"]))

        color = (0, 255, 255) if pos_idx == state.selected_idx else (0, 0, 255)

        cv2.circle(canvas, (x, y), POINT_RADIUS, color, -1)
        cv2.circle(canvas, (x, y), POINT_RADIUS + 2, (255, 255, 255), 1)

        # 1. Point sequence identity number tags
        cv2.putText(canvas, str(pos_idx + 1), (x + 9, y - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(canvas, str(pos_idx + 1), (x + 9, y - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)

        # 2. Dynamic [j, i] pixel value coordinate string tags
        coord_text = f"[{x}, {y}]"
        cv2.putText(canvas, coord_text, (x + 9, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(canvas, coord_text, (x + 9, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (50, 50, 50), 1, cv2.LINE_AA)

    x1, y1, x2, y2 = button_rect(state)

    cv2.rectangle(canvas, (x1, y1), (x2, y2), (40, 160, 60), -1)
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (20, 90, 30), 2)

    cv2.putText(
        canvas,
        "GEOREFERENCE",
        (x1 + 18, y1 + 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    help_text = "Drag points | A:add point | D:delete | S:save CSV | G/button:georef | Q/Esc:quit"

    if state.add_mode:
        cv2.putText(
            canvas,
            "ADD MODE ACTIVE - Click on image to place point",
            (230, state.image_height + 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.putText(
        canvas,
        help_text,
        (230, state.image_height + 45),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )

    cv2.imshow(WINDOW_NAME, canvas)


def add_point(state: AppState, x: int, y: int) -> None:
    try:
        X = float(input(f"Enter X coordinate for pixel ({x}, {y}): "))
        Y = float(input(f"Enter Y coordinate for pixel ({x}, {y}): "))
    except ValueError:
        print("Invalid input. Please enter numeric values.")
        return

    state.points.loc[len(state.points)] = {
        "j": x,
        "i": y,
        "X": X,
        "Y": Y,
    }
    # Reset index mapping sequence sequence securely
    state.points = state.points.reset_index(drop=True)
    state.selected_idx = len(state.points) - 1

    print(f"Added point {state.selected_idx + 1}: j={x}, i={y}, X={X}, Y={Y}")


def mouse_callback(event, x, y, flags, param):
    state: AppState = param

    if y >= state.image_height:
        if event == cv2.EVENT_LBUTTONDOWN and is_inside_button(state, x, y):
            state.georeference_requested = True
        return

    if event == cv2.EVENT_LBUTTONDOWN:
        if state.add_mode:
            add_point(state, x, y)
            state.add_mode = False
            return

        idx = nearest_point(state.points, x, y)
        if idx is not None:
            state.selected_idx = idx
            state.dragging = True
            print(
                f"Selected point {idx + 1}: "
                f"j={state.points.iloc[idx]['j']}, "
                f"i={state.points.iloc[idx]['i']}, "
                f"X={state.points.iloc[idx]['X']}, "
                f"Y={state.points.iloc[idx]['Y']}"
            )

    elif event == cv2.EVENT_MOUSEMOVE:
        # FIX: Mutate coordinate space targets using positional column mappings
        if state.dragging and state.selected_idx is not None:
            j_col_idx = state.points.columns.get_loc("j")
            i_col_idx = state.points.columns.get_loc("i")
            state.points.iat[state.selected_idx, j_col_idx] = x
            state.points.iat[state.selected_idx, i_col_idx] = y

    elif event == cv2.EVENT_LBUTTONUP:
        state.dragging = False


def run_editor(state: AppState) -> bool:
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WINDOW_NAME, mouse_callback, state)

    print("Controls:")
    print("  Drag point        : adjust pixel position")
    print("  A                 : activate add point mode (then click on image)")
    print("  D                 : delete selected point")
    print("  S                 : save CSV")
    print("  G or button       : perform georeferencing")
    print("  Q or Esc          : quit")

    while True:
        draw_ui(state)
        key = cv2.waitKey(30) & 0xFF

        if state.georeference_requested or key == ord("g"):
            save_points(state.points, state.csv_path)
            return True

        elif key == ord("a"):
            state.add_mode = True
            print("Add Point Mode activated. Click anywhere on the map image.")

        elif key == ord("s"):
            save_points(state.points, state.csv_path)

        elif key == ord("d") and state.selected_idx is not None:
            print(f"Deleted point {state.selected_idx + 1}")
            state.points = state.points.drop(index=state.selected_idx).reset_index(drop=True)
            state.selected_idx = None

        elif key == ord("q") or key == 27:
            return False


def geo_referencing_engine(image_path: Path, csv_path: Path, output_img_path: Path):
    img = cv2.imread(str(image_path))

    if img is None:
        raise FileNotFoundError(f"Unable to load image: {image_path}")

    points = load_points(csv_path)

    state = AppState(
        image_bgr=img,
        csv_path=csv_path,
        output_path=output_img_path,
        points=points,
    )

    should_georef = run_editor(state)
    cv2.destroyAllWindows()

    if not should_georef:
        print("Closed without georeferencing.")
        return

    height, width = img.shape[:2]
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    create_georeferenced_raster(
        state.points,
        raster_size=(width, height),
        output_path=output_img_path,
        image_data=img_rgb,
    )

    print(f"Georeferenced raster saved to: {output_img_path}")


def main():
    image_path: Path = INPUT_DIR / "georeferencing/raw" / "geology.png"
    csv_path: Path = INPUT_DIR / "georeferencing/raw" / "geology.csv"
    output_path: Path = INPUT_DIR / "georeferencing/processed/geology.tif"
    geo_referencing_engine(image_path, csv_path, output_path)


if __name__ == "__main__":
    main()