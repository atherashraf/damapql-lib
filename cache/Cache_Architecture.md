# DAMapQL Cache Architecture

## 1. Purpose

DAMapQL uses a shared caching architecture designed to support multiple applications and multiple GIS service types while keeping cache ownership isolated between deployments.

The immediate priority is **MVT caching**. The architecture is intentionally structured so that TMS, WMS, raster-processing, or other cache types can be introduced later without redesigning the application-level cache infrastructure.

SQLite is **not part of the cache architecture**. Redis is the cache backend.

---

## 2. Design Principles

The cache architecture follows these principles:

- Redis is the application cache backend.
- Every deployed application has its own cache namespace.
- The application namespace is derived from the application identity/configuration.
- MVT-specific caching remains separate from generic cache infrastructure.
- Cache failures should not make the underlying GIS service unavailable.
- Cache invalidation must be namespace-aware.
- Administrative cache clearing must affect only the current application.
- Future TMS and WMS caches should reuse the same infrastructure and namespace strategy.
- Domain-specific cache implementations should only be introduced when required.

---

## 3. Current Structure

The current architecture should be understood as:

```text
DAMapQL Application
        │
        ├── app_bootstrap.py
        │       └── application startup / cache availability
        │
        ├── cache.py
        │       └── generic Redis cache infrastructure
        │
        ├── mvt_cache.py
        │       └── MVT-specific Redis caching
        │
        └── manage.py
                └── administrative cache management
```

The responsibilities of these modules should remain clearly separated.

---

## 4. `cache.py` — Generic Cache Infrastructure

**Reference:** `app/damapql/cache/cache.py`

`cache.py` is the common cache infrastructure layer.

Its responsibilities include:

- Redis configuration
- Redis client initialization
- application cache namespace
- common TTL configuration
- generic Redis cache operations
- shared cache error handling
- common namespace-based cache management utilities

It must remain independent of GIS service-specific concepts.

Therefore, `cache.py` should not contain knowledge of:

- MVT XYZ coordinates
- MVT variants
- PostGIS MVT generation
- WMS BBOX requests
- TMS rendering parameters
- layer-specific rendering rules

This separation allows the same Redis infrastructure to support additional cache domains later.

---

## 5. Application Namespace

Every DAMapQL-based application must have its own Redis namespace.

Typical deployments include:

```text
WASA   → wasa
LBDC   → lbdc
PULSE  → pulse
```

The namespace represents **application ownership** of Redis keys.

Conceptually:

```text
Redis
│
├── wasa:*
├── lbdc:*
├── pulse:*
└── other_application:*
```

This allows several applications to safely share the same Redis infrastructure.

The fundamental rule is:

> **An application may manage only keys belonging to its own namespace.**

---

## 6. Service Namespace

Inside the application namespace, cache entries are further separated by service type.

Current:

```text
{application}:mvt:...
```

Future examples:

```text
{application}:tms:...
{application}:wms:...
{application}:raster:...
{application}:api:...
```

Lock entries follow the same ownership model:

```text
{application}:lock:mvt:...
```

The general convention is:

```text
{application}:{service}:{resource...}
```

and for locks:

```text
{application}:lock:{service}:{resource...}
```

This provides two clear levels of organization:

```text
Application ownership
        │
        ▼
Service/cache type
        │
        ▼
Cached resource
```

---

## 7. `mvt_cache.py` — Current Primary Cache

**Reference:** `app/damapql/cache/mvt_cache.py`

MVT is currently the primary domain-specific cache.

`mvt_cache.py` is responsible for MVT-specific concepts such as:

- layer identity
- XYZ tile coordinates
- tile variants
- MVT byte storage
- MVT cache lookup
- MVT cache invalidation
- distributed tile-generation locking
- MVT-specific Redis key construction

The MVT cache should not duplicate generic Redis configuration that belongs in `cache.py`.

The conceptual relationship is:

```text
cache.py
   │
   │ generic Redis infrastructure
   ▼
mvt_cache.py
   │
   │ MVT-specific identity and behavior
   ▼
Redis
```

---

## 8. MVT Cache Identity

An MVT tile is not uniquely identified by XYZ coordinates alone.

The same layer and tile coordinates may produce different output depending on request parameters such as:

- selected columns
- resolution
- tile size
- feature count or limits
- additional query/rendering parameters

Therefore, MVT caching uses a deterministic **variant** in addition to:

```text
layer
z
x
y
```

Conceptually:

```text
{application}:mvt:{layer}:{variant}:{z}:{x}:{y}
```

This prevents one MVT representation from incorrectly satisfying another request for the same XYZ coordinate.

---

## 9. MVT Request Flow

The intended request flow is:

```text
MVT Request
     │
     ▼
Build Tile Variant
     │
     ▼
Check Redis MVT Cache
     │
 ┌───┴────┐
 │        │
HIT      MISS
 │        │
 ▼        ▼
Return   Acquire Tile Lock
Tile          │
              ▼
        Recheck Cache
              │
         ┌────┴────┐
         │         │
        HIT       MISS
         │         │
         ▼         ▼
       Return    PostGIS
                  │
                  ▼
              Generate MVT
                  │
                  ▼
              Store Redis
                  │
                  ▼
              Release Lock
                  │
                  ▼
              Return Tile
```

The second cache lookup after lock acquisition is important because another worker may have generated the tile while the current request was waiting.

---

## 10. Distributed MVT Locking

MVT generation may be relatively expensive, particularly for large spatial datasets or complex PostGIS queries.

Multiple application workers may request the same uncached tile at approximately the same time.

The Redis MVT cache therefore supports distributed tile-generation locking.

Conceptually:

```text
Without lock

Worker 1 ──► PostGIS
Worker 2 ──► PostGIS
Worker 3 ──► PostGIS
Worker 4 ──► PostGIS
```

With locking:

```text
Worker 1 ──► Lock ──► PostGIS ──► Redis
Worker 2 ──► Cache recheck
Worker 3 ──► Cache recheck
Worker 4 ──► Cache recheck
```

Lock ownership must be protected so that one worker cannot accidentally release a lock currently owned by another worker.

---

## 11. Redis Is an Optimization, Not the GIS Data Source

Redis should improve performance but should not become the authoritative source for GIS data.

The authoritative data remains in the underlying data source, such as PostGIS.

Conceptually:

```text
Client
  │
  ▼
Cache
  │
  ├── HIT  → return cached result
  │
  └── MISS
        │
        ▼
   Authoritative Data
        │
        ▼
   Generate Response
        │
        ▼
      Cache
```

If Redis is unavailable, the service should be capable of continuing through its normal data-generation path where appropriate.

---

## 12. `app_bootstrap.py` — Application Startup Integration

**Reference:** `app/damapql/app_bootstrap.py`

`app_bootstrap.py` is the application startup integration point for cache infrastructure.

Its role is not to implement caching itself.

Instead, it should be responsible for application-level startup concerns such as:

- initializing the DAMapQL application
- checking Redis availability
- reporting cache connectivity status
- logging whether Redis caching is available
- allowing the application to continue according to the configured cache behavior

The separation should remain:

```text
app_bootstrap.py
      │
      │ startup / health awareness
      ▼
cache.py
      │
      │ cache infrastructure
      ▼
Redis
```

`app_bootstrap.py` should not contain MVT key-generation or tile-cache implementation logic.

That remains the responsibility of `mvt_cache.py`.

---

## 13. `manage.py` — Administrative Cache Management

**Reference:** `app/damapql/manage.py`

`manage.py` provides administrative and maintenance commands for the application.

It should include an application-level cache-clear operation.

The purpose of this operation is:

> Clear all cached data belonging to the **current application namespace**, without affecting any other application sharing Redis.

For example, when the current application is WASA:

```text
Target:

wasa:*
```

The following must remain untouched:

```text
lbdc:*
pulse:*
other_application:*
```

This operation is intentionally broader than MVT-specific cache clearing.

Today it may primarily clear:

```text
wasa:mvt:...
wasa:lock:mvt:...
```

In the future, the same administrative operation can also clear:

```text
wasa:tms:...
wasa:wms:...
wasa:raster:...
wasa:api:...
```

without changing the fundamental management concept.

---

## 14. Application-Level vs Domain-Level Cache Clearing

Two levels of cache invalidation should remain conceptually separate.

### Domain-level invalidation

Handled by the relevant cache implementation.

Examples:

```text
Clear one MVT layer
Clear all MVT tiles
Clear one future TMS layer
Clear WMS entries for a layer
```

### Application-level invalidation

Handled through `manage.py`.

Example:

```text
Clear everything belonging to WASA
```

Conceptually:

```text
manage.py
    │
    ▼
{current_application}:*
```

This separation prevents administrative concerns from being embedded inside individual GIS cache implementations.

---

## 15. Shared Redis Deployment

The architecture supports a single Redis service shared by several DAMapQL applications.

```text
                         Redis
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
      WASA                LBDC              PULSE
    wasa:*               lbdc:*             pulse:*
        │                  │                  │
        ▼                  ▼                  ▼
       MVT                MVT                MVT
```

Future:

```text
                         Redis
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
      WASA                LBDC              PULSE
        │                  │                  │
   ┌────┼────┐        ┌────┼────┐        ┌────┼────┐
   │    │    │        │    │    │        │    │    │
  MVT  TMS  WMS      MVT  TMS  WMS      MVT  TMS  WMS
```

Namespace isolation ensures that administration of one deployment does not disturb another.

---

## 16. Future TMS Cache

TMS caching is not required in the current implementation.

When needed, it should be introduced as a separate domain cache rather than extending MVT-specific logic.

**Future reference:**

```text
app/damapql/cache/tms_cache.py
```

The TMS cache may need to account for parameters such as:

- layer
- style
- image format
- tile size
- time
- filters
- XYZ coordinates

Conceptual namespace:

```text
{application}:tms:...
```

The generic Redis configuration and application namespace should continue to come from `cache.py`.

---

## 17. Future WMS Cache

WMS caching should also remain a separate domain implementation.

**Future reference:**

```text
app/damapql/cache/wms_cache.py
```

WMS cache identity can be substantially more complex than MVT because rendered output may depend on:

- layers
- BBOX
- width
- height
- CRS
- output format
- styles
- transparency
- time
- filters
- vendor-specific parameters

Conceptual namespace:

```text
{application}:wms:...
```

A normalized request identity or deterministic request hash should be used when WMS caching is implemented.

---

## 18. Future Raster Cache

If expensive raster operations require caching later, they should have their own domain implementation.

**Future reference:**

```text
app/damapql/cache/raster_cache.py
```

Potential cache identity may include:

- raster/layer identity
- operation
- spatial extent
- resolution
- bands
- time
- processing parameters

Conceptual namespace:

```text
{application}:raster:...
```

Raster-processing cache identities should not be mixed with MVT cache identities.

---

## 19. Future API Cache

Generic API response caching may also be introduced later if required.

**Future reference:**

```text
app/damapql/cache/api_cache.py
```

Potential identity may include:

- endpoint
- query parameters
- API/data version
- relevant access context

Conceptual namespace:

```text
{application}:api:...
```

User- or permission-sensitive responses require special care so cached content is never incorrectly shared across authorization boundaries.

---

## 20. Future Package Structure

The intended evolution is:

```text
cache/
│
├── cache.py
│   └── generic Redis infrastructure
│
├── mvt_cache.py
│   └── current MVT cache
│
├── tms_cache.py
│   └── future TMS cache
│
├── wms_cache.py
│   └── future WMS cache
│
├── raster_cache.py
│   └── future raster-processing cache
│
└── api_cache.py
    └── future API response cache
```

Application integration remains outside the cache package:

```text
app_bootstrap.py
    └── startup / Redis availability

manage.py
    └── administrative namespace cache clearing
```

---

## 21. Responsibility Matrix

| Component | Responsibility |
|---|---|
| `cache.py` | Generic Redis infrastructure, namespace and common cache behavior |
| `mvt_cache.py` | MVT-specific cache identity, tile storage and distributed locking |
| `app_bootstrap.py` | Startup integration and Redis availability awareness |
| `manage.py` | Administrative clearing of the current application's cache namespace |
| `tms_cache.py` | Future TMS-specific caching |
| `wms_cache.py` | Future WMS-specific caching |
| `raster_cache.py` | Future raster-processing caching |
| `api_cache.py` | Future generic API response caching |

---

## 22. Cache Namespace Strategy

The recommended hierarchy is:

```text
{application}:{service}:{resource}
```

Examples:

```text
wasa:mvt:...
wasa:tms:...
wasa:wms:...

lbdc:mvt:...
lbdc:tms:...
lbdc:wms:...

pulse:mvt:...
pulse:tms:...
pulse:wms:...
```

Locks use:

```text
{application}:lock:{service}:{resource}
```

For example:

```text
wasa:lock:mvt:...
```

This hierarchy makes Redis keys easy to understand, isolate, inspect, and invalidate.

---

## 23. Cache Clearing Safety

Application-level cache clearing must always be namespace-scoped.

For WASA:

```text
wasa:*
```

For LBDC:

```text
lbdc:*
```

For PULSE:

```text
pulse:*
```

Global Redis clearing operations must not be used for normal DAMapQL application maintenance because Redis may be shared by multiple applications.

The management operation should therefore remain namespace-based regardless of how many cache domains are added later.

---

## 24. Current Implementation Priority

The recommended priority is:

```text
Generic Redis Infrastructure
          │
          ▼
    Application Namespace
          │
          ▼
       MVT Cache
          │
          ▼
 Distributed MVT Lock
          │
          ▼
 app_bootstrap Integration
          │
          ▼
 manage.py Cache Management
```

Only after a real requirement appears should the architecture expand to:

```text
TMS
WMS
Raster
API
```

---

## 25. Final Architecture

```text
                       DAMapQL Application
                              │
               ┌──────────────┴──────────────┐
               │                             │
        app_bootstrap.py                 manage.py
       Startup / Redis health       Administrative clearing
               │                             │
               └──────────────┬──────────────┘
                              │
                              ▼
                           cache.py
                    Generic Redis Layer
                              │
                              ▼
                         mvt_cache.py
                      MVT-Specific Cache
                              │
                              ▼
                            Redis
```

Future:

```text
                           cache.py
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
     mvt_cache.py        tms_cache.py        wms_cache.py
       Current              Future              Future
          │                   │                   │
          └───────────────────┼───────────────────┘
                              │
                            Redis
```

The central design rule is:

> **The application namespace defines cache ownership, while the service namespace defines the type of cached resource.**

This keeps the current MVT implementation focused while providing a clean path for future TMS, WMS, raster, and API caching without introducing unnecessary abstractions today.
