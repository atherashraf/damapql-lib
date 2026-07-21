import cv2
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.transform import Affine
from shapely.geometry import Point
import os
from settings import MEDIA_DIR


def rgb_to_hsv(rgb):
    """Convert an RGB tuple to HSV."""
    color = np.uint8([[rgb]])
    hsv = cv2.cvtColor(color, cv2.COLOR_RGB2HSV)
    return hsv[0][0]


def get_hsv_values():
    """Determine the HSV range for pink points based on given RGB values."""
    # Given RGB values
    # pink points rgb range 103 wells
    # rgb_values = [(227, 3, 255), (223, 6, 247), (232, 0, 253),
    #               (235, 0, 255), (228, 4, 255), (234, 1, 253)]

    rgb_values = [(0,234,0),(0,230,0),(0,227,2),
                  (0,226,0),(3,222,0),(1,230,7),
                  (0,232,0),(0,229,2),(9,229,9)]

    # Convert the RGB values to HSV
    hsv_values = [rgb_to_hsv(rgb) for rgb in rgb_values]

    # Calculate the range (adjust as needed)
    lower_hsv = np.array([min(h[0] for h in hsv_values) - 10, 50, 50])
    upper_hsv = np.array([max(h[0] for h in hsv_values) + 10, 255, 255])

    return lower_hsv, upper_hsv


def extract_pixel_coordinates(input_img):
    """Extract one centroid for each pink point."""
    lower_hsv, upper_hsv = get_hsv_values()

    # Load the georeferenced image
    image = cv2.imread(input_img)

    # Convert the image to HSV color space
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    # Create a mask for the pink color
    mask = cv2.inRange(hsv_image, lower_hsv, upper_hsv)

    # Find contours of the pink points
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Extract the pixel coordinates of the centroids
    pixel_coords = []
    for contour in contours:
        M = cv2.moments(contour)
        if M["m00"] != 0:
            # Calculate the centroid
            cX = int(M["m10"] / M["m00"])  # X coordinate
            cY = int(M["m01"] / M["m00"])  # Y coordinate
            pixel_coords.append((cX, cY))

    return pixel_coords


def extract_features(input_img, output_shapefile):
    """Convert pixel coordinates to geographic coordinates and save as GeoPackage."""
    pixel_coords = extract_pixel_coordinates(input_img)

    # Open the georeferenced raster with Rasterio
    with rasterio.open(input_img) as src:
        transform: Affine = src.transform  # Get the affine transformation
        crs = src.crs  # Get the CRS (coordinate reference system)

    # Convert pixel coordinates to geographic coordinates
    geo_coords = []
    for px, py in pixel_coords:
        lon, lat = rasterio.transform.xy(transform, py, px, offset="center")
        geo_coords.append((lon, lat))

    # Create a GeoDataFrame
    gdf = gpd.GeoDataFrame(
        {"geometry": [Point(x, y) for x, y in geo_coords]},
        crs=crs  # Use the CRS from the georeferenced image
    )

    # Save to GeoPackage
    gdf.to_file(output_shapefile)

    print(f"Saved {len(gdf)} points to {output_shapefile}")


if __name__ == "__main__":
    input_img = os.path.join(MEDIA_DIR, "georeferencing/Map_29wells_georef.tif")
    output_shp = os.path.join(MEDIA_DIR, "georeferencing/Map_29wells.shp")

    extract_features(input_img, output_shp)
