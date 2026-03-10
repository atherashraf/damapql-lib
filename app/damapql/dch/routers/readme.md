Good structure. With these files, I’d place the endpoints like this.

## `maps.py`

Keep map definitions and layer catalog metadata here.

Use for:

* `/all_layer_info/`
* `/all_map_info/`
* `/save_map/`
* `/get_map_info/{uuid}/`
* `/delete_map/{uuid}/`
* `/delete_layerinfo_row/{uuid}/`
* `/get_all_layers/`
* `/layer_info/{uuid}/`
* `/layer_extent/{uuid}/`

Your current `maps.py` already fits this well.

---

## `styles.py`

Anything about style save/load/upload/download.

Use for:

* `/get_style/{uuid}/{map_uuid}/`
* `/save_style/{uuid}/{map_uuid}/`
* `/upload_sld_style/{uuid}/`
* `/download_da_style/{uuid}/`
* `/get_legend_graphic/{uuid}`

---

## `layers.py`

Anything about querying layer data, fields, attributes, WFS, feature detail.

Use for:

* `/wfs/{uuid}/{format}/`
* `/layer_fields/{uuid}/`
* `/layer_field_distinct_values/{uuid}/{field_name}/{field_type}/`
* `/layer_attributes/{uuid}/`
* `/get_feature_detail/{uuid}/{col_name}/{col_val}/`
* `/get_feature_geometry/{uuid}/{pk_values}/`
* `/column_value/{uuid}/{pk_val}/{col_name}/`

This file is usually the biggest after raster.

---

## `raster.py`

Anything tile/raster/pixel/band/raster upload/cache if raster-specific.

Use for:

* `/raster_tile/{uuid}/{z}/{x}/{y}/`
* `/get_pixel_value/{uuid}/{long}/{lat}/`
* `/get_raster_info/{uuid}/`
* `/add_raster_layer_info`
* `/raster_data_model_types`
* maybe `/layer_mvt/{uuid}/{tile_size}/{z}/{x}/{y}` only if you want all tile-serving here

I would keep `layer_mvt` here too because it is tile-serving infrastructure.

---

## `admin.py`

Admin/config/metadata/model management endpoints.

Use for:

* `/save_layer_info/`
* `/add_model_row/`
* `/get_db_connection/`
* `/layer_categories/`
* `/add_layer_url_info/{layer_title}/{layer_category_id}/{layer_url}/{url_type}/`
* `/clear_cache/{uuid}/`

If you want stricter organization, `clear_cache` could be in `raster.py`, but `admin.py` is fine too.

---

## `navigation.py`

Navigation tree and geometry.

Use for:

* `/navigation_list/{map_uuid}/`
* `/navigation_geometry/{map_uuid}/{selected_key}/{node_id}/`

---

## `export.py`

Download/export/send operations.

Use for:

* `/send-email/`
* maybe future CSV/Excel/GeoJSON export endpoints
* if you later add “download map config”, “download attributes”, they belong here

Right now `send-email` could also live in `admin.py`, but `export.py` is acceptable if you plan more outward-facing output/download endpoints.

---

# My exact recommendation for your current endpoints

### `maps.py`

* all_layer_info
* all_map_info
* save_map
* get_map_info
* delete_map
* delete_layerinfo_row
* get_all_layers
* layer_info
* layer_extent

### `styles.py`

* get_style
* save_style
* upload_sld_style
* download_da_style
* get_legend_graphic

### `layers.py`

* wfs
* layer_fields
* layer_field_distinct_values
* layer_attributes
* get_feature_detail
* get_feature_geometry
* column_value

### `raster.py`

* raster_tile
* get_pixel_value
* get_raster_info
* add_raster_layer_info
* raster_data_model_types
* layer_mvt

### `admin.py`

* save_layer_info
* add_model_row
* get_db_connection
* layer_categories
* add_layer_url_info
* clear_cache
* upload_shp_file

### `navigation.py`

* navigation_list
* navigation_geometry

### `export.py`

* send-email

---

# Small refinement

`upload_shp_file` could go either in:

* `admin.py`, or
* `layers.py`

I would put it in `admin.py` if it is mainly an authenticated ingestion/management endpoint.
I would put it in `layers.py` if you think of it as layer creation.

For your project, `admin.py` feels better.

---

# Suggested `__init__.py`

```python
from fastapi import APIRouter

from .maps import dch_maps_router
from .styles import dch_styles_router
from .layers import dch_layers_router
from .raster import dch_raster_router
from .admin import dch_admin_router
from .navigation import dch_navigation_router
from .export import dch_export_router

dch_router = APIRouter(tags=["dch"])

dch_router.include_router(dch_maps_router)
dch_router.include_router(dch_styles_router)
dch_router.include_router(dch_layers_router)
dch_router.include_router(dch_raster_router)
dch_router.include_router(dch_admin_router)
dch_router.include_router(dch_navigation_router)
dch_router.include_router(dch_export_router)
```

---

# Practical next step

The best next file to implement is `styles.py`, because it is compact and easy to separate cleanly.
