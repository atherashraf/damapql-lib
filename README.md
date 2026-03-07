
# DAMapQL

**DAMapQL** (DigitalArz Map Query Layer) is a **FastAPI-based geospatial backend** designed to power modern web GIS applications.

It provides APIs for:

* Map management
* Layer management
* Vector and raster data access
* Vector tiles (MVT)
* Raster tiles
* Styling (JSON / SLD)
* Navigation hierarchies
* Spatial attribute queries
* Raster analytics
* Authentication and role management

The project is part of the **DigitalArz platform** and is designed to work with **OpenLayers / DAMap React map components**.

---

# Architecture

DAMapQL follows a **modular router architecture**.

```
app/
 ├── auth/          Authentication & authorization
 └── dch/           Digital Cartography Hub (GIS services)

app/dch/routers/
 ├── maps.py        Map configuration APIs
 ├── layers.py      Vector layer metadata & attributes
 ├── styles.py      Layer styling APIs
 ├── tiles.py       Vector tile (MVT) and cache APIs
 ├── raster.py      Raster processing APIs
 ├── navigation.py  Administrative boundary navigation
 ├── admin.py       Administrative operations
 └── export.py      Export utilities
```

---

# Key Features

## Authentication

User authentication and authorization are handled via:

```
app/auth/
```

Capabilities:

* JWT authentication
* Role-based access control
* Department-based permissions
* User role management

Supported roles include:

```
superuser
app_manager
data_editor
survey_manager
```

---

# GIS Capabilities

## Map Management

```
/dch/all_map_info
/dch/save_map
/dch/get_map_info
/dch/delete_map
```

Allows:

* Saving map configuration
* Managing map layers
* Layer ordering and visibility
* Base layer configuration

---

## Layer Management

```
/dch/all_layer_info
/dch/layer_info
/dch/get_all_layers
/dch/layer_fields
/dch/layer_attributes
```

Supports:

* Vector metadata
* Field inspection
* Attribute table queries
* Feature details
* Distinct value classification

---

## Styling System

```
/dch/get_style
/dch/save_style
/dch/upload_sld_style
/dch/download_da_style
/dch/get_legend_graphic
```

Supports:

* JSON style definitions
* SLD styles
* Map-specific styling overrides
* Legend generation

---

## Vector Tiles (MVT)

```
/dch/layer_mvt/{uuid}/{z}/{x}/{y}
```

Features:

* PostGIS vector tile generation
* Tile caching
* Column whitelisting
* Resolution control

---

## Raster Tiles

```
/dch/raster_tile/{uuid}/{z}/{x}/{y}
```

Supports:

* Cloud Optimized GeoTIFF
* Dynamic color mapping
* On-the-fly tile generation

---

## Raster Analytics

```
/dch/get_pixel_value
/dch/get_raster_info
```

Capabilities:

* Pixel value extraction
* Band statistics
* Raster metadata inspection

---

## Navigation System

```
/dch/navigation_list
/dch/navigation_geometry
```

Provides hierarchical spatial navigation:

Example:

```
Country
 └ Province
     └ District
         └ Tehsil
```

---

# Vector Data Services

## WFS

```
/dch/wfs/{uuid}/{format}
```

Features:

* GeoJSON output
* Column selection
* Pagination
* CRS transformation

---

# Layer Upload

## Shapefile Upload

```
/dch/upload_shp_file
```

Capabilities:

* Upload shapefile components
* Import into PostGIS
* Auto metadata creation

---

## Raster Upload

```
/dch/add_raster_layer_info
```

Features:

* Raster upload
* Automatic COG conversion
* Metadata registration

---

# Tile Cache

Vector tiles can be cached in a dedicated database.

```
/dch/clear_cache/{uuid}
```

Cache tables follow the naming convention:

```
{layer_uuid}_xyz
```

---

# Project Structure

```
DAMapQL/
│
├── app/
│   ├── auth/
│   │   ├── manager/
│   │   │   ├── email_manager.py
│   │   │   └── role_manager.py
│   │   ├── dtypes.py
│   │   ├── models.py
│   │   ├── routers.py
│   │   ├── utils.py
│   │   └── __init__.py
│   │
│   ├── dch/
│   │   ├── fixtures/
│   │   │   ├── layercategory.json
│   │   │   └── layerqualitylevel.json
│   │   │
│   │   ├── routers/
│   │   │   ├── admin.py
│   │   │   ├── export.py
│   │   │   ├── layers.py
│   │   │   ├── maps.py
│   │   │   ├── navigation.py
│   │   │   ├── raster.py
│   │   │   ├── styles.py
│   │   │   ├── tiles.py
│   │   │   └── __init__.py
│   │   │
│   │   ├── utils/
│   │   │   ├── dch_utils.py
│   │   │   ├── df_utils.py
│   │   │   ├── email_utils.py
│   │   │   ├── mvt_utils.py
│   │   │   └── web_mercator_tiling.py
│   │   │
│   │   ├── api_enum.py
│   │   ├── dch_map_schema.py
│   │   ├── dtypes.py
│   │   ├── models.py
│   │   ├── scripts.py
│   │   ├── tree.sql
│   │   └── __init__.py
│   │
│   └── __init__.py
│
├── tests/
│   ├── auth/
│   ├── dch/
│   └── conftest.py
│
├── main.py
├── manage.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
└── README.md
```

---

# Testing

DAMapQL includes **pytest-based API tests**.

Run tests:

```bash
pytest -v
```

Example output:

```
14 passed, 0 failed
```

Test folders:

```
tests/auth
tests/dch
```

Tests include:

* router testing
* role permission validation
* mocked database sessions
* authentication override

---

# Dependencies

Core libraries used:

```
FastAPI
SQLAlchemy
GeoPandas
Shapely
Rasterio
JenksPy
Matplotlib
PyProj
Pandas
```

---

# Running the Application

Install dependencies:

```bash
pip install -r requirements.txt
```

Run server:

```bash
uvicorn app.main:app --reload
```

API documentation:

```
http://localhost:8000/docs
```

---

# Future Enhancements

Planned improvements:

* Redis tile caching
* Async DB queries
* STAC support
* Vector tile pre-generation
* Layer permission system
* OGC API Features

---

# License

Internal DigitalArz project.

---

# Author

**Ather Ashraf**
Associate Professor – Software Engineering
PUCIT – University of the Punjab

Specialization:

* GeoSpatial AI
* Intelligent GIS
* Spatial Data Infrastructure
* Remote Sensing Analytics

