
# DAMapQL End-to-End Data Architecture: Ingestion, Access & Tiling

## 1. System Overview

DAMapQL unifies **Raster (`R`)**, **Vector (`V`)**, **Tabular (`T`)**, and **Document (`D`)** assets behind a single catalog schema (`LayerInfo` + 1:1 `LayerAccessibility`), identical MinIO and local `MEDIA_BASE` directory hierarchies, zero-copy `/vsis3/` streaming, and Redis-cached XYZ tile rendering.

```text
================================================================================
 1. INGESTION LAYER (app/damapql/dch/services/ingestion/)
================================================================================
                        PipelineIngestService
                  (Declarative JSON Batch Gateway)
                                 │
      ┌──────────────────┬───────┴────────┬──────────────────┐
      ▼                  ▼                ▼                  ▼
RasterIngestService VectorIngestService Tabular Handler  Document Handler
  (DataModel.R)       (DataModel.V)     (DataModel.T)    (DataModel.D)
      │                  │                │                  │
      ▼                  ▼                ▼                  ▼
COG Validation     Multi-Engine Router  JSON/CSV → Arrow  MIME & Byte Size
 & Conversion      ┌───┬───────┬────┐   Parquet (Snappy)     Inspection
      │            │   │       │    │         │                  │
      │         PostGIS FGB Parquet GPKG      │                  │
      │            │   └───┬───┴────┘         │                  │
      └────────────┼───────┼──────────────────┴──────────────────┘
                   │       │
                   ▼       ▼
        PostGIS Spatial   Managed Object / File Storage
           Database       (MinIO S3  ≡  Local MEDIA_BASE)
        ('data' DB Key)   <cat_main>[/<cat_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>
                   │       │
                   └───┬───┘
                       ▼
              Catalog Database ('dch')
            ┌──────────────────────────┐
            │  1:1 LayerAccessibility  │
            │            ▲             │
            │            │ (CASCADE)   │
            │        LayerInfo         │
            └────────────┬─────────────┘
                         │
================================================================================
 2. ACCESSIBILITY LAYER (app/damapql/dch/services/layer_accessibility/)
================================================================================
                         │
           ┌─────────────┴─────────────┐
           ▼                           ▼
  RasterAccessService         VectorAccessService
           │                           │
    ┌──────┴──────┐        ┌───────────┼───────────┐
    ▼             ▼        ▼           ▼           ▼
 MinIO COG    Local COG  PostGIS   /vsis3/ FGB   Local / Cached
(/vsis3/s3)  (MEDIA_BASE) Engine   & GeoParquet   GPKG & Files
    │             │        │           │           │
    └──────┬──────┘        └───────────┼───────────┘
           │                           │
================================================================================
 3. TILING & RENDERING LAYER (app/damapql/dch/services/layer_tiling/)
================================================================================
           ▼                           ▼
   RasterTileService           VectorTileService
   (XYZ → PNG Tiles)           (XYZ → MVT Protobuf)
           │                           │
           └─────────────┬─────────────┘
                         ▼
               MVTCache (Redis + Locks)
             Variant-Keyed Tile Caching

```

---

## 2. Catalog Data Models & Taxonomy (`api_enum.py`)

All ingestion and read services resolve against a single shared set of enums:

| `DataModel` Code | Name | Supported `ModelType` Values | Normalized Storage Formats |
| --- | --- | --- | --- |
| **`R`** | Raster | `Satellite`, `DEM`, `DSM`, `Surface`, `Classification`, `Other` | Cloud-Optimized GeoTIFF (`COG` / `.tif`) |
| **`V`** | Vector | `Geom`, `Network`, `Contour`, `Tin`, `Other` | PostGIS (`MVT`), FlatGeobuf (`.fgb`), GeoParquet (`.parquet`), GeoPackage (`.gpkg`) |
| **`T`** | Tabular | `Table`, `Telemetry`, `Other` | Snappy-compressed Apache Parquet (`.parquet`) |
| **`D`** | Document | `PDF`, `DOC`, `Txt`, `Latex`, `Other` | Native binary/text (`.pdf`, `.docx`, `.txt`, `.tex`, `.xlsx`, etc.) |
| **`URL`** | External URL | `Api`, `Application`, `Other` | External service endpoint |
| **`Application`** | App Layer | `Application`, `Other` | Registered application layer |

---

## 3. Unified Storage Hierarchy & Naming Convention

### Landing vs. Managed Storage

1. **Staging Storage (`StagingStorage`):** Transient workspace (`LocalStagingStorage` or ephemeral MinIO staging) holding raw incoming single files or multi-file shapefile bundles (`.shp`, `.shx`, `.dbf`, `.prj`, `.zip`) to provide a valid POSIX path for GDAL/Rasterio/PyOGRIO conversions. Cleaned up unconditionally in `finally` blocks.
2. **Managed Storage (Cloud & Local Parity):** Durable storage in MinIO (`bucket_name`) and local filesystem fallback (`MEDIA_BASE`) share the **exact same relative path** built by `LayerInfoService.build_layer_object_name(...)` and `DCHUtils.build_versioned_object_name(...)`:

```text
<category_main>[/<category_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>

```

* **MinIO Accessibility (`AccessibilityType.Minio`):**
* `bucket`: `<bucket_name>` (e.g., `wasa` or `dch`)
* `object_name`: `<relative_key>` (never prefixed with bucket name)


* **Local File Accessibility (`AccessibilityType.File`):**
* Physical path: `<MEDIA_BASE>/<relative_key>`
* `file_path`: `<relative_key>` with `path_base="media_rel"` (validated via `Path.is_relative_to(MEDIA_BASE)`)



### Example Object Keys

* **Vector (`.parquet`):** `Boundaries/Catchment/Geom/wasa_lbdc_phase3_ccas_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_123.parquet`
* **Vector (`.fgb`):** `Boundaries/Catchment/Geom/wasa_lbdc_phase3_ccas_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_124.fgb`
* **Raster (`.tif`):** `Elevation/DEM/DEM/srtm_basin_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_456.tif`
* **Tabular (`.parquet`):** `Hydrology/Telemetry/Table/canal_discharge_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_789.parquet`
* **Document (`.pdf`):** `Reports/Hydrology/PDF/lbdc_audit_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_999.pdf`

---

## 4. Ingestion Services (`services/ingestion/`)

### 4.1 `RasterIngestService`

* **Entry Points:** `ingest_upload(...)` (multipart `UploadFile`) and `ingest_from_path(...)` (existing filesystem path).
* **Normalization:** Verifies CRS presence (`validate_raster`), converts non-COG GeoTIFFs via `COGRaster.create_cog(...)`, uploads/copies to managed storage, and validates the persisted COG (`is_valid_cog()`) before registering `LayerAccessibility` and `LayerInfo` (`format="COG"`).

### 4.2 `VectorIngestService` & Storage Engines

* **Entry Points:** `ingest_upload(...)`, `ingest_from_path(...)`, and reusable `persist_vector(...)` (shared with `TempLayerInfo` routers).
* **Validation & Normalization:** Unpacks `.zip` archives safely (preventing ZipSlip traversal), enforces valid CRS and single geometry family, lowercases column names, renames geometry to `geom`, and computes WGS84 (`extent`) and Web Mercator (`extent_3857`) bounding boxes.
* **Pluggable Storage Engines (`BaseVectorStorageEngine`):**

| Engine | Driver / Format | Key Persistence Behavior |
| --- | --- | --- |
| **`PostgisVectorEngine`** | PostGIS (`MVT`) | Writes to spatial DB (`DCH_DATA_DB_KEY`), strips Z/M coordinates, repairs invalid rings (`make_valid`), promotes mixed `Polygon`/`MultiPolygon` to `MultiPolygon`, adds `dch_id SERIAL PRIMARY KEY` + GiST index, and enforces PostgreSQL `max_len=54` table naming. |
| **`FlatGeobufVectorEngine`** | `FlatGeobuf` (`.fgb`) | Reprojects to EPSG:4326 and writes with Packed Hilbert R-Tree spatial index (`spatial_index=True`) for fast HTTP range queries. |
| **`GeoParquetVectorEngine`** | `GeoParquet` (`.parquet`) | Writes GeoParquet 1.1+ with `write_covering_bbox=True`, `snappy` compression, and `row_group_size=10000` for `/vsis3/` row-group pruning. |
| **`GeoPackageVectorEngine`** | `GPKG` (`.gpkg`) | Writes OGC GeoPackage with named layer (`safe_title`) and typed column metadata. |

### 4.3 `PipelineIngestService` (Declarative Batch Gateway)

* Accepts `PipelineBatchRequest` JSON payloads containing mixed `R`, `V`, `T`, and `D` items (`source_path` or `inline_records`).
* **Tabular (`DataModel.T`):** Normalizes JSON arrays/dicts, CSVs, or Parquet files via Pandas + PyArrow into Snappy Parquet with embedded `schema_definition` and `row_count`.
* **Document (`DataModel.D`):** Detects MIME type and byte size, infers `ModelType` from extension (`.pdf` $\rightarrow$ `PDF`, `.doc`/`.docx` $\rightarrow$ `DOC`, `.txt`/`.md` $\rightarrow$ `Txt`, `.tex`/`.latex` $\rightarrow$ `Latex`), and persists to MinIO or `MEDIA_BASE`.
* **Execution Modes:**
* `atomic=False`: Isolates each layer; failures roll back only that item and record `status="FAILED"` in `PipelineBatchResult`.
* `atomic=True`: Tracks all external side effects (`_CreatedArtifact`: MinIO objects, local files, PostGIS tables) across layers and rolls back both SQLAlchemy state and all prior external artifacts if any layer fails.



---

## 5. Accessibility Services (`services/layer_accessibility/`)

Decouples physical storage backends from downstream query and tiling services:

### 5.1 `RasterAccessService`

* **`open_cog(info, minio_manager)`**:
* For `Minio` / `S3` records: Opens via `COGRaster.open_from_minio(params, minio_manager)`.
* For `File` records: Resolves `MEDIA_BASE`-relative (`path_base="media_rel"`) or absolute paths with directory-traversal checks and opens via `COGRaster.open_from_local(...)`.



### 5.2 `VectorAccessService`

* **`resolve(info, minio_manager)` $\rightarrow$ `VectorTarget**`:
1. **PostGIS (`VectorSourceFormat.POSTGIS`):** Resolves external SQLAlchemy `Engine`, `table_name`, `geom_col`, `columns`, and `pk_sql_fragment`.
2. **MinIO FGB / GeoParquet (`FGB`, `GEOPARQUET`):** Configures GDAL S3 environment variables (`configure_vsis3`) and returns `/vsis3/<bucket>/<object_name>` for zero-copy HTTP range streaming.
3. **Local Files & Cached GeoPackage (`GPKG`, `File`):** Resolves `MEDIA_BASE` paths or downloads MinIO `.gpkg` objects once into `CACHE_ROOT` using per-object `threading.Lock` and atomic `os.replace(...)`.


* **`read_gdf(target, bbox_4326, requested_cols)`**:
* Caches native file CRS (`_crs_cache`) via `pyogrio.read_info` and transforms `bbox_4326` into native CRS before reading.
* Pushes down spatial `bbox` filtering and attribute `columns` selection directly to `pyogrio` / Arrow so only required columns and intersecting row groups / R-tree leaves are fetched over `/vsis3/`.
* Provides `evict_cached_file(info)` to purge local `.gpkg` cache files and `_crs_cache` entries on cache invalidation.



---

## 6. Tile Rendering & Caching (`services/layer_tiling/` & `cache/mvt_cache.py`)

### 6.1 `RasterTileService` (`XYZ` $\rightarrow$ `image/png`)

* Enforces `LayerInfo.zoom_range` bounds (returning empty `b""` outside zoom range).
* Builds deterministic cache variants keyed by `bucket`, `source` (`object_name` or `file_path`), `tile_size`, and an MD5 hash of `custom_color_map` / `LayerInfo.layer_styling`.
* Uses distributed Redis tile locks (`acquire_tile_lock`) to prevent stampedes on concurrent cold-tile requests, then renders PNG tiles via `COGRaster.read_tile_as_png(...)`.

### 6.2 `VectorTileService` (`XYZ` $\rightarrow$ `application/x-protobuf` MVT)

* Supports three resolution modes:
1. `LayerInfo` UUID (PostGIS, FlatGeobuf, GeoParquet, GeoPackage)
2. Registered SQLAlchemy ORM model (`model_name`)
3. Direct PostGIS table (`table_name` + `db_key`)


* Preserves explicit geometry-only tile requests (`cols=[]` $\rightarrow$ `requested_cols=[]`) vs. full-attribute requests (`cols=None`).
* Computes zoom-dependent simplification resolution (`78271.52 / 2**z`), checks `MVTCache`, acquires distributed tile locks, and generates MVT bytes either via PostGIS `ST_AsMVT` (`DBQuery.execute_query_as_mvt` / `MVTUtils.get_tile_bytes`) or via `VectorAccessService.read_gdf(...)` + `MVTUtils.gdf_to_mvt(...)`.

---

## 7. Operational Invariants & Future Evolution

### Core Invariants

* **Strict 1:1 Ownership:** Every `LayerInfo` owns a dedicated `LayerAccessibility` row (`CASCADE` delete); no two layers share an accessibility record.
* **Transactional Rollback Guarantee:** No failed ingestion leaves orphaned MinIO objects, local files, or PostGIS tables behind.
* **Bounded Memory Streaming:** Multipart uploads stream in fixed `1 MB` chunks (`BUFFER_SIZE = 1024 * 1024`).

### Evolution Path: Synchronous $\rightarrow$ Distributed Workers

```text
Current Synchronous Flow:
  FastAPI Endpoint ─▶ StagingStorage ─▶ Sync Normalization ─▶ MinIO / PostGIS ─▶ DB Commit

Future Distributed Flow:
  FastAPI Endpoint ─▶ MinIO Staging (`staging/raw/...`)
                          │
                          ▼
                   RabbitMQ / Redis Queue
                          │
                          ▼
               Celery GDAL / GeoArrow Worker
               (mkdtemp ─▶ COG / GeoParquet ─▶ MinIO Managed ─▶ DB Commit ─▶ Staging Cleanup)

```

```

```