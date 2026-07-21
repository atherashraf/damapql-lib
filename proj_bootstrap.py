"""
Zero-import PROJ/GDAL bootstrap for Windows to avoid PostGIS/OSGeo conflicts.

MUST be imported before anything geospatial:
rasterio, pyproj, geopandas, fiona, shapely CRS ops, osgeo.gdal.

Do not import pyproj/rasterio here. We only set paths.
"""

import os
import sys
import pathlib
import platform


def _find_site_packages() -> pathlib.Path:
    for p in sys.path:
        if p and ("site-packages" in p or "dist-packages" in p):
            return pathlib.Path(p)

    return pathlib.Path(sys.prefix) / "Lib" / "site-packages"


def _has_bad_gis_path(path_str: str) -> bool:
    s = str(path_str).lower()
    return (
        "postgresql" in s
        or "postgis" in s
        or "osgeo4w" in s
        or "qgis" in s
    )


def _add_dir(p: pathlib.Path):
    if not p or not p.exists():
        return

    p_str = str(p)

    current_path = os.environ.get("PATH", "")
    if p_str not in current_path:
        os.environ["PATH"] = p_str + ";" + current_path

    if platform.system() == "Windows":
        try:
            os.add_dll_directory(p_str)
        except Exception:
            pass


# --------------------------------------------------
# Locate venv site-packages
# --------------------------------------------------
sp = _find_site_packages()

pyproj_pkg = sp / "pyproj"
pyproj_proj_bin = pyproj_pkg / "proj_dir" / "bin"
pyproj_proj_db = pyproj_pkg / "proj_dir" / "share" / "proj"

rasterio_pkg = sp / "rasterio"
rasterio_proj_db = rasterio_pkg / "proj_data"
rasterio_gdal = rasterio_pkg / "gdal_data"

# --------------------------------------------------
# Remove conflicting external GIS paths/envs
# --------------------------------------------------
for key in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA"):
    v = os.environ.get(key, "")
    if v and _has_bad_gis_path(v):
        os.environ.pop(key, None)

if "PATH" in os.environ:
    os.environ["PATH"] = ";".join(
        p for p in os.environ["PATH"].split(";")
        if p and not _has_bad_gis_path(p)
    )

# --------------------------------------------------
# Add venv GIS DLL/data locations
# Prefer Rasterio/GDAL paths first
# --------------------------------------------------
_add_dir(rasterio_pkg)
_add_dir(rasterio_proj_db)
_add_dir(rasterio_gdal)
_add_dir(pyproj_proj_bin)
_add_dir(sp)

# --------------------------------------------------
# Set PROJ database
# Prefer Rasterio's proj.db because Rasterio/GDAL CRS
# operations require the matching database version.
# --------------------------------------------------
if (rasterio_proj_db / "proj.db").exists():
    os.environ["PROJ_LIB"] = str(rasterio_proj_db)
    os.environ["PROJ_DATA"] = str(rasterio_proj_db)

elif (pyproj_proj_db / "proj.db").exists():
    os.environ["PROJ_LIB"] = str(pyproj_proj_db)
    os.environ["PROJ_DATA"] = str(pyproj_proj_db)

# --------------------------------------------------
# Set GDAL data
# --------------------------------------------------
if rasterio_gdal.exists():
    os.environ["GDAL_DATA"] = str(rasterio_gdal)

os.environ.setdefault("PROJ_NETWORK", "ON")

# Optional debug
# os.environ["PROJ_DEBUG"] = "3"

PROJ_DIR = os.environ.get("PROJ_DATA")
GDAL_DIR = os.environ.get("GDAL_DATA")