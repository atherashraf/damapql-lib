Here is the updated, complete Markdown architecture documentation reflecting your refactored **Unified Ingestion & Accessibility Architecture** across **Raster, Vector, Tabular, and Document** datasets, your standardized `<category_main>[/<category_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>` layout, and your multi-engine / atomic rollback guarantees:

```markdown
# DAMapQL Ingestion & Accessibility Architecture

## Broad Concept

DAMapQL unifies **Raster (`R`)**, **Vector (`V`)**, **Tabular (`T`)**, and **Document (`D`)** ingestion behind a consistent normalization, persistence, and catalog registration pipeline, paired with zero-copy `/vsis3/` streaming and local `MEDIA_BASE` parity.

```text
                               PipelineIngestService
                         (Declarative JSON Batch Gateway)
                                        │
        ┌───────────────────┬───────────┴───────────┬───────────────────┐
        ▼                   ▼                       ▼                   ▼
RasterIngestService VectorIngestService   Tabular Processor   Document Processor
   (DataModel.R)       (DataModel.V)        (DataModel.T)       (DataModel.D)
        │                   │                       │                   │
        ▼                   ▼                       ▼                   ▼
 COG Validation     Multi-Engine Router      JSON/CSV → Arrow     MIME & Size
  & Conversion      ┌───┬───────┬─────┐     Parquet (Snappy)       Inspection
        │           │   │       │     │             │                   │
        │        PostGIS FGB Parquet GPKG           │                   │
        │           │   └───┬───┴─────┘             │                   │
        └───────────┼───────┼───────────────────────┴───────────────────┘
                    │       │
                    ▼       ▼
         PostGIS Spatial   Managed Object / File Storage
            Database       (MinIO S3  ≡  Local MEDIA_BASE)
         ('data' DB Key)   <cat_main>[/<cat_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>
                    │       │
                    └───┬───┘
                        ▼
               Transactional Catalog
             ┌────────────────────────┐
             │ 1:1 LayerAccessibility │
             │          ▲             │
             │          │ (CASCADE)   │
             │      LayerInfo         │
             └────────────────────────┘

```

---

## Storage Layer Separation & Unified Naming

The pipeline enforces strict separation between transient landing zones and immutable, versioned durable artifacts:

* **Staging Storage (`StagingStorage`):** Temporary workspace (`LocalStagingStorage` or ephemeral MinIO staging). Holds raw incoming single files or multi-file shapefile bundles (`.shp`, `.shx`, `.dbf`, `.prj`, `.zip`) to provide a valid POSIX file path for GDAL/Rasterio/PyOGRIO conversions. Automatically cleaned up in `finally` blocks.
* **Managed / Persistent Storage (Cloud & Local Parity):** Permanent home for normalized Cloud-Optimized GeoTIFFs (`.tif`), FlatGeobuf (`.fgb`), GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Tabular Parquet (`.parquet`), and Documents (`.pdf`, `.docx`, `.txt`, `.tex`, etc.).
* **Unified Relative Path Layout:** Both MinIO (`object_name`) and Local Disk (`MEDIA_BASE` with `path_base="media_rel"`) use the exact same relative path structure:
```text
<category_main>[/<category_sub>]/<model_type>/<slug>_<uuid>_YYYYMMDD_HHMMSS_fff.<ext>

```


* **Example Keys:**
* Vector: `Boundaries/Catchment/Geom/wasa_lbdc_phase3_ccas_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_123.parquet`
* Raster: `Elevation/DEM/DEM/srtm_basin_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_456.tif`
* Tabular: `Hydrology/Telemetry/Table/canal_discharge_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_789.parquet`
* Document: `Reports/Hydrology/PDF/lbdc_audit_861834d4df8d11f1a2a5cc5807d2daf0_20261114_124500_999.pdf`




* **Database Catalog (`dch` + `data`):**
* Registers dedicated, non-shared 1:1 `LayerAccessibility` records bound directly to `LayerInfo` entities.
* For `PostgisVectorEngine`, spatial geometries are written into the target PostGIS database (`DCH_DATA_DB_KEY`) with `dch_id SERIAL PRIMARY KEY`, 2D sanitized geometries, and GiST spatial indexing (`max_len=54` identifier safety).



```text
                                INGESTION
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │   StagingStorage    │
                        │ (Transient Landing) │
                        └──────────┬──────────┘
                                   │
                         ┌─────────┴─────────┐
                         ▼                   ▼
                    Local Stage         MinIO Stage
                         │                   │
                         └─────────┬─────────┘
                                   │
                                   ▼
                    NORMALIZATION & VALIDATION
               COG / Hilbert FGB / GeoParquet 1.1+
               GPKG / PostGIS / Snappy Parquet
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │   Managed Storage   │
                        │ (Durable Artifacts) │
                        └──────────┬──────────┘
                         ┌─────────┼─────────┐
                         ▼         ▼         ▼
                    File Storage  MinIO   PostGIS
                     MEDIA_BASE   Bucket  ('data')
                         └─────────┼─────────┘
                                   ▼
                        ┌─────────────────────┐
                        │   Catalog DB (dch)  │
                        │ ─────────────────── │
                        │  LayerAccessibility │
                        │          ▲          │
                        │   1:1    │          │
                        │      LayerInfo      │
                        └─────────────────────┘

```

---

## Read & Tiling Access Layer (`layer_accessibility` & `layer_tiling`)

Stored layers are resolved uniformly at query/tile time without duplicating storage logic in routers:

* **`RasterAccessService` & `RasterTileService`:**
* Resolves MinIO/S3 (`COGRaster.open_from_minio`) or `MEDIA_BASE`-relative/absolute local paths (`COGRaster.open_from_local`).
* Applies zoom-range filtering, custom/catalog color maps, and Redis tile caching (`mvt_cache`) keyed by object version and style hash.


* **`VectorAccessService` & `VectorTileService`:**
* Resolves `VectorTarget` across **PostGIS**, **FlatGeobuf**, **GeoParquet**, and **GeoPackage**.
* Streams FGB and GeoParquet directly from MinIO via GDAL `/vsis3/` HTTP range reads with native CRS caching, bounding-box row-group/R-tree pruning, and attribute column pushdown (`pyogrio`).
* Downloads and caches SQLite-backed GeoPackages locally with per-object thread locks and atomic file replacement.



---

## Deletion & Ingestion Invariants

* **Strict 1:1 Accessibility:** Each catalog asset owns its dedicated `LayerAccessibility` record. Deleting a `LayerInfo` row cleanly cascades to its accessibility record without affecting other layers.
* **Single-Layer & Cross-Layer Atomic Rollbacks:**
* Individual ingestion services (`RasterIngestService`, `VectorIngestService`) automatically delete newly uploaded MinIO objects, local `MEDIA_BASE` files, or PostGIS tables if validation or catalog commits fail.
* `PipelineIngestService` tracks all external artifacts (`_CreatedArtifact`) across multi-layer `atomic=True` batch runs and rolls back both SQLAlchemy state and all prior MinIO/local/PostGIS artifacts on failure.


* **Resource Safety:** Upload streaming uses fixed `1MB` chunk buffers (`BUFFER_SIZE = 1024 * 1024`) to maintain flat memory usage for multi-gigabyte rasters and vector archives.

---

## Architectural Evolution

### Current Synchronous Workflow

```text
FastAPI Endpoint (Single Upload or PipelineBatchRequest)
       ↓
StagingStorage (Local / MinIO)
       ↓
Synchronous Normalization (COG / Vector Engine / Parquet / Document)
       ↓
Managed Storage Persistence (MinIO / MEDIA_BASE / PostGIS)
       ↓
DB Commit (LayerInfo + 1:1 LayerAccessibility)

```

### Future Asynchronous / Distributed Pipeline

```text
FastAPI Endpoint
       ↓
MinIO Staging (`staging/raw/...`)
       ↓
Task Broker (RabbitMQ / Redis)
       ↓
Celery / Distributed GDAL & GeoArrow Worker
       ↓
Local Scratch Mount (`mkdtemp`)
       ↓
Normalization (COG Pyramiding / GeoParquet Covering BBox)
       ↓
MinIO Managed Storage (<cat_main>[/<cat_sub>]/<model_type>/<slug>_<uuid>_<ts>.<ext>)
       ↓
DB Commit (LayerInfo + LayerAccessibility)
       ↓
MinIO Staging Cleanup & Redis Tile Cache Invalidation

```

```

```