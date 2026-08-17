# Ingestion Architecture
## Broad concept
```text
                         RasterIngestService
                                │
                ┌───────────────┴───────────────┐
                │                               │
          ingest_upload()                ingest_from_path()
                │                               │
                ▼                               ▼
         Staging Storage                 Persistent Storage
                │                               │
             Process                        Resolve object
                │                               │
                └───────────────┬───────────────┘
                                ▼
                         Metadata Reader
                                │
                                ▼
                         Register LayerInfo
```

## Upload Ingestion Architecture
```text
                         INGESTION
                            │
                            ▼
                 ┌─────────────────────┐
                 │   StagingStorage    │
                 └──────────┬──────────┘
                            │
                  ┌─────────┴─────────┐
                  ▼                   ▼
             Local Stage         MinIO Stage
                  │                   │
                  └─────────┬─────────┘
                            │
                            ▼
                    PROCESSING
                GDAL / Rasterio / COG
                            │
                            ▼
                 ┌─────────────────────┐
                 │  Managed Storage    │
                 └──────────┬──────────┘
                            │
                  ┌─────────┴─────────┐
                  ▼                   ▼
             File Storage           MinIO
              MEDIA_BASE          Permanent
```

# Future Evolution
Now 
```text

FastAPI
   ↓
StagingStorage
   ↓
Processing
   ↓
ManagedStorage
```
Future
```text
FUTURE

FastAPI
   ↓
MinIO Staging
   ↓
RabbitMQ
   ↓
Worker
   ↓
local scratch
   ↓
Processing
   ↓
ManagedStorage
```

