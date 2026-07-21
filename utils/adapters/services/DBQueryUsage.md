# DBQuery Utility Usage Guide

This guide demonstrates how to use the various data extraction methods provided by the `DBQuery` utility.

---

## 1. Standard API Responses (Pydantic)

Used for standard REST endpoints where you want validated JSON data based on your Pydantic schemas.

```python
# Returns a single validated Pydantic object
user = DBQuery.execute_query_as_pydantic(session, select(User).where(User.id == 1))

# Returns a list of validated Pydantic objects
active_projects = DBQuery.execute_query_all_as_pydantic(
    session, 
    select(Project).where(Project.is_active == True)
)

```

---

## 2. High-Performance Web Mapping (GeoJSON)

Uses PostGIS native functions to aggregate spatial data. Ideal for displaying features on a map via Leaflet or OpenLayers.

```python
# Postgres path: returns a single FeatureCollection dict directly from the DB
geojson_data = DBQuery.execute_query_as_geojson(
    session, 
    select(Parcel).limit(100), 
    geom_col="geom", 
    id_col="parcel_id"
)

```

---

## 3. Large Scale Map Tiling (MVT)

Used for vector tiles (Protobuf). This is the standard for rendering thousands/millions of points smoothly in Mapbox GL JS.

```python
# Returns binary bytes for a specific tile (Z/X/Y)
tile_bytes = DBQuery.execute_query_as_mvt(
    session,
    stmt=select(Infrastructure),
    z=14, x=8642, y=5721,
    layer_name="water_mains"
)

```

---

## 4. Data Science & Analysis (GeoPandas GDF)

Used when you need to perform backend spatial operations like buffering, spatial joins, or overlays.

```python
# Returns a GeoDataFrame with Shapely geometry objects
gdf = DBQuery.execute_query_as_gdf(
    session, 
    select(District), 
    geom_col="geom", 
    crs="EPSG:4326"
)

# You can now do: gdf.buffer(100) or gdf.area

```

---

## 5. Reporting & Pivot Tables (Pandas DataFrame)

Used for data cleaning, statistical analysis, or generating Excel/CSV exports.

```python
# Returns a standard Pandas DataFrame
# enum_columns cleans SQLAlchemy Enums to simple strings automatically
df = DBQuery.execute_stmt_as_df(
    session, 
    stmt="SELECT name, status, created_at FROM orders",
    enum_columns=["status"]
)

```

---

## 6. Paginated Lists (Metadata)

Used for frontend data tables that require "Total Pages" and "Next/Prev" controls.

```python
# Returns a dict with 'items' and 'metadata'
paged_data = DBQuery.paginate_query(
    session, 
    query=select(Log), 
    page=1, 
    page_size=50
)

# Access metadata: paged_data['metadata']['total_pages']

```

---

## 7. Strict Single Row (Dictionary)

Used for business logic where you expect exactly one result and want to avoid list indexing.

```python
# Returns a plain dict or None
user_dict = DBQuery.execute_query_as_one(
    session, 
    "SELECT * FROM users WHERE email = :email", 
    params={"email": "admin@wasa.gov"},
    raise_if_multiple=True # Error if data integrity is compromised
)

```

---

### Which Method Should I Use?

| If you need to... | Use this Method |
| --- | --- |
| Send data to a **Standard UI List** | `execute_query_all_as_pydantic` |
| Send data to a **Paginated Table** | `paginate_query` |
| Show **100 points** on a web map | `execute_query_as_geojson` |
| Show **1 million points** on a map | `execute_query_as_mvt` |
| Create a **CSV/Excel Export** | `execute_stmt_as_df` |
| Do **Spatial Joins/Buffers** in Python | `execute_query_as_gdf` |

**Would you like me to show you how to combine these into a specialized "Search Service" that handles multiple formats dynamically?**