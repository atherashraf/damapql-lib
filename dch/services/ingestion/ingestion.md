
```markdown
# Ingestion Architecture

## Broad Concept

```text
                         RasterIngestService
                                │
                ┌───────────────┴───────────────┐
                │                               │
          ingest_upload()                ingest_from_path()
                │                               │
                ▼                               ▼
          StagingStorage                 Source Asset (Local)
        (Local / MinIO)                         │
                │                               │
                ▼                               ▼
       COG Validation/Convert         COG Validation/Convert
                │                               │
                └───────────────┬───────────────┘
                                │
                                ▼
                       Persistent Storage
                   (MinIO S3 / Local MEDIA_BASE)
                                │
                                ▼
                         Metadata Reader
                      (Bounds, CRS, SRID, Stats)
                                │
                                ▼
                     Transactional Database
                    ┌────────────────────────┐
                    │ 1:1 LayerAccessibility │
                    │          ▲             │
                    │          │ (CASCADE)   │
                    │      LayerInfo         │
                    └────────────────────────┘

```

---

## Storage Layer Separation

The pipeline enforces strict separation between landing zones and durable storage:

* **Staging Storage (`StagingStorage`):** Temporary workspace. Holds raw incoming uploads (in local scratch or ephemeral MinIO buckets) to provide a valid POSIX file path for GDAL/Rasterio conversions. Automatically cleaned up in `finally` blocks.
* **Persistent / Managed Storage:** Permanent home for verified Cloud-Optimized GeoTIFFs (COGs). Stored under deterministic keys (`app/main_category/sub_category/filename_uuid_date.tif`).
* **Database Catalog:** Registers dedicated, non-shared 1:1 `LayerAccessibility` records bound directly to `LayerInfo` entities.

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
                           PROCESSING & COG
                         GDAL / Rasterio / COG
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │   Managed Storage   │
                        │ (Durable Artifacts) │
                        └──────────┬──────────┘
                                   │
                         ┌─────────┴─────────┐
                         ▼                   ▼
                    File Storage           MinIO
                     MEDIA_BASE       Permanent Bucket
                                   │
                                   ▼
                        ┌─────────────────────┐
                        │ Database (PostGIS)  │
                        │ ─────────────────── │
                        │  LayerAccessibility │
                        │          ▲          │
                        │   1:1    │          │
                        │      LayerInfo      │
                        └─────────────────────┘

```

---

## Deletion & Ingestion Invariants

* **Strict 1:1 Accessibility:** Each raster layer owns its dedicated `LayerAccessibility` record. Deleting a `LayerInfo` row cleanly drops its associated accessibility record without affecting other layers.
* **Atomic Rollbacks:** If validation, COG conversion, or database commits fail at any stage, newly created MinIO objects and local COG files are automatically rolled back.
* **Resource Safety:** File descriptor streaming uses fixed chunk buffers (`1MB`) to ensure stable memory consumption for multi-gigabyte raster files.

---

## Architectural Evolution

### Current Synchronous Workflow

```text
FastAPI Endpoint
       ↓
StagingStorage (Local / MinIO)
       ↓
Synchronous Worker (COG Creation & Metadata Extraction)
       ↓
Managed Storage Persistence
       ↓
DB Commit (LayerInfo + LayerAccessibility)

```

### Future Asynchronous / Distributed Pipeline

```text
FastAPI Endpoint
       ↓
MinIO Staging (`staging/raw/...`)
       ↓
Task Broker (RabbitMQ / Redis)
       ↓
Celery / Distributed GDAL Worker
       ↓
Local Scratch Mount (`mkdtemp`)
       ↓
COG Conversion & Pyramiding
       ↓
MinIO Managed Storage (`app/category/...`)
       ↓
DB Commit (LayerInfo + LayerAccessibility)
       ↓
MinIO Staging Cleanup & Cache Invalidation

```

```

```