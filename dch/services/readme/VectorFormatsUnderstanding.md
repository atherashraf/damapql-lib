# Vector Formats Understanding

| Format | Primary Role in `damap_be` | Why It Fits |
| --- | --- | --- |
| **PostGIS** | **Live Transactional Editing & Spatial Joins** | Best when rows are continuously inserted, updated, or deleted via API/forms, or when running multi-table SQL spatial joins, triggers, and real-time aggregations. |
| **FlatGeobuf (`.fgb`)** | **Immutable Snapshots, Versioning & Fast Streaming** | Built-in Hilbert R-tree spatial index lets GDAL `/vsis3/` stream bounding-box slices over HTTP Range requests with very low latency. Ideal for versioned map releases (e.g., Phase 1 / Phase 2 / Phase 3 boundaries) where geometry is read-only. |
| **GeoParquet (`.parquet`)** | **Versioned Snapshots + Columnar / In-Memory Analytics** | Columnar compression + predicate pushdown makes it much faster and smaller than `.fgb` when running GeoPandas/DuckDB/Arrow analytics, filtering specific attribute columns (`?cols=...`), or summarizing millions of rows without touching PostGIS. |
| **GeoPackage (`.gpkg`)** | **Portable Desktop Exchange / Multi-Layer Container** | Best treated as a fallback or desktop interoperability format (QGIS/ArcGIS offline packages, or bundling multiple layers + styles in one file), since SQLite's random-access B-tree header seeks are slower over `/vsis3/` than `.fgb` or `.parquet`. |

### Two Practical Tips to Keep Versioning & GeoParquet Clean

1. **Immutable Object Keys for Versioning in MinIO**:
* Store each version under a distinct `object_name` (e.g., `boundaries/ccas_v1.fgb`, `boundaries/ccas_v2.fgb`) rather than overwriting the same key in place. Because `VectorTileService` hashes `target_table` (`/vsis3/...`) into `mvt_cache.build_variant(...)`, switching a layer's `object_name` automatically isolates its tile cache without stale-tile collisions.


2. **GeoParquet `read_parquet` Column Pushdown**:
* In `VectorAccessService.read_gdf`, passing `columns=...` to `gpd.read_parquet(source, bbox=read_bbox, columns=...)` when `requested_cols` is provided allows Arrow to read **only** the requested attribute columns plus geometry over `/vsis3/`, skipping the remaining columns in MinIO completely.



Would you like to add column pushdown to `VectorAccessService.read_gdf` for GeoParquet and inspect your GeoParquet upload/registration flow next?