---

# LayerInfo.md

# LayerInfo – Unified Spatial Layer Registry

## Overview

`LayerInfo` is the central registry of DAMapQL. It provides a unified abstraction for all spatial datasets regardless of how they are stored or accessed.

Rather than distinguishing between database layers, raster datasets, GeoPackages, Cloud Optimized GeoTIFFs (COGs), web services, or application models, every spatial resource is represented as a `LayerInfo`.

The framework separates **logical layer metadata** from **physical data accessibility**, allowing the same APIs to operate on heterogeneous spatial data sources.

```
                 LayerInfo
                     │
      ┌──────────────┼──────────────┐
      │              │              │
  Vector         Raster         External
      │              │              │
      ▼              ▼              ▼
 LayerAccessibility LayerAccessibility LayerAccessibility
```

---

# Core Design Philosophy

DAMapQL separates two responsibilities.

## LayerInfo

Describes **what the layer is**.

It contains

* identity
* metadata
* projection
* geometry
* styling
* categories
* temporal information
* quality
* analytical properties

LayerInfo never stores how data is physically accessed.

---

## LayerAccessibility

Describes **where the data lives and how it is accessed**.

Examples include

* PostgreSQL/PostGIS
* GeoPackage
* Cloud Optimized GeoTIFF
* GeoTIFF
* Shapefile
* WFS
* ArcGIS REST
* XYZ Tiles
* WMTS
* WMS
* remote URL
* local filesystem

LayerInfo and LayerAccessibility are intentionally independent.

```
LayerInfo
     │
     ▼
LayerAccessibility
     │
     ▼
Actual data source
```

---

# LayerInfo Creation Paths

DAMapQL currently supports three primary onboarding workflows.

---

## 1. Application Models

Application-managed SQLAlchemy models can automatically generate LayerInfo records.

```
python -m app.damapql.manage create_layerinfo_from_app_models \
    --app_label wasa
```

Typical examples include

* assets
* parcels
* canals
* complaints
* inspections
* administrative boundaries

Internally

```
SQLAlchemy Model
        │
        ▼
save_app_layer_infos()
        │
        ▼
LayerInfo
```

Advantages

* automatic discovery
* synchronized with ORM
* consistent metadata
* application aware

---

## 2. Vector Datasets

External vector datasets can be onboarded independently from SQLAlchemy.

Examples

```
GeoPackage

Shapefile

PostGIS

File Geodatabase

FlatGeobuf

GeoJSON
```

Future command

```
python -m app.damapql.manage create_layerinfo_from_vector \
    --file parcels.gpkg \
    --layer_name Parcels
```

The workflow

```
Vector Dataset
      │
      ▼
Geometry inspection
      │
      ▼
LayerAccessibility(DB/File)
      │
      ▼
LayerInfo
```

---

## 3. Raster Datasets

Raster datasets are registered through

```python
from app.damapql.dch.models import create_layerinfo_from_raster
create_layerinfo_from_raster(
    title="LULC 2024",
    raster_path="media/rasters/lulc_2024.tif",
)
```

Examples

* GeoTIFF
* COG
* DEM
* DSM
* Land Cover
* NDVI
* Climate grids

Workflow

```
Raster
    │
    ▼
save_raster_layer_info()
    │
    ▼
LayerInfo
```

---

# Runtime Access

Once registered, every dataset is accessed through the same LayerInfo API.

---

## Vector

```
layer.to_layer_gdf()

layer.to_layer_df()

layer.count_layer_rows()

layer.get_row_detail()
```

---

## Raster

```
layer.to_raster()

layer.to_cog()

layer.to_layer_df()
```

Raster DataFrames automatically adapt.

Single band

```
value

count
```

Multiband

```
band

statistics

histograms
```

---

# Layer Accessibility Types

LayerAccessibility stores the physical access definition.

---

## Database

```
{
    "conn_name": "...",
    "table_name": "...",
    "columns": [...],
    "g_col": "geom",
    "pk_cols": ["id"]
}
```

---

## File

```
{
    "file_path": "...",
    "is_path_media_rel": true
}
```

---

## URL

```
{
    "url": "...",
    "url_type": "..."
}
```

Examples

* WMS
* WMTS
* XYZ
* ArcGIS REST
* OGC API
* COG URL

---

# LayerInfo Metadata

LayerInfo stores the logical metadata common to all spatial datasets.

```
UUID

Application

Layer Name

Title

Description

Data Model

Model Type

SRID

Geometry Type

Native Extent

EPSG:3857 Extent

Spatial Resolution

Temporal Resolution

Category

Quality

Styling

Visibility

Zoom Levels

Tags

Additional Metadata
```

---

# Data Models

DAMapQL currently supports

```
Vector

Raster
```

Future models may include

```
Point Cloud

Mesh

3D Tiles

Voxel

Network

Time Series
```

without changing LayerInfo.

---

# Unified Architecture

```
                    LayerInfo
                        │
      ┌─────────────────┼──────────────────┐
      │                 │                  │
 Application        Vector Dataset     Raster Dataset
      │                 │                  │
 SQLAlchemy         File / Database      GeoTIFF / COG
      │                 │                  │
      └─────────────────┼──────────────────┘
                        │
                LayerAccessibility
                        │
       ┌────────────────┼────────────────┐
       │                │                │
   Database          File             URL
       │                │                │
       └────────────────┼────────────────┘
                        │
                  Unified Layer API
```

---

# Layer Registration Lifecycle

```
Spatial Dataset
        │
        ▼
Layer Discovery
        │
        ▼
Metadata Extraction
        │
        ▼
LayerAccessibility Creation
        │
        ▼
LayerInfo Creation
        │
        ▼
Metadata Reconciliation
        │
        ▼
Available to Maps
        │
        ▼
Available to Analytics
        │
        ▼
Available to APIs
```

---

# Metadata Reconciliation

Because spatial datasets evolve over time, DAMapQL treats the underlying data source as the authoritative source.

A reconciliation process validates and updates LayerInfo metadata against the actual dataset.

Typical corrections include:

* geometry column (`g_col`)
* primary key columns
* attribute columns
* geometry type(s)
* SRID
* native extent
* EPSG:3857 extent
* row counts
* accessibility parameters

Example:

```bash
python -m app.damapql.manage reconcile_layerinfo

python -m app.damapql.manage reconcile_layerinfo \
    --layer_uuid <uuid>
```

This ensures that LayerInfo remains synchronized with the physical data source even after schema changes.

---

# Benefits

The LayerInfo architecture provides:

* **Unified abstraction** for all spatial datasets.
* **Separation of metadata and accessibility**, allowing storage mechanisms to evolve independently.
* **Consistent APIs** for maps, analytics, and services.
* **Automatic onboarding** from application models, vector datasets, and raster datasets.
* **Metadata reconciliation** to keep registry information synchronized with changing data sources.
* **Extensibility**, enabling future support for additional spatial data models and access mechanisms without redesigning the framework.

---
