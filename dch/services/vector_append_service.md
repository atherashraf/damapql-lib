
---

# 📌 1. Put THIS at top of your class

```python

    """
    Generic Shapefile → PostGIS Append Service

    This service provides a fully schema-driven pipeline to append vector data
    (GeoDataFrame) into an existing PostGIS table safely and consistently across
    multiple applications (e.g., WASA, Pulse, SOS, Flood, DCH, etc.).

    ------------------------------------------------------------------------
    🔑 Core Principles
    ------------------------------------------------------------------------

    1. Schema-Driven (No Hardcoding)
       - All behavior is derived from target table schema (nullable, defaults, geometry, SRID).
       - No table-specific logic is required.

    2. Database Defaults Are Respected
       - Columns with server-side defaults are NOT inserted if missing.
       - This allows PostgreSQL to apply defaults automatically.

    3. No NULL Injection
       - Missing optional columns are NEVER filled with None.
       - Columns are omitted instead.

    4. Strict Validation for Required Fields
       - Columns that are NOT NULL and have NO default must be provided.
       - Validation happens before insertion.

    5. Safe Handling of Default Columns
       - If a column has a DB default:
            • All values NULL → column is dropped → DB default applies
            • Partial NULL values → rejected (ambiguous behavior)
            • No NULL values → inserted normally

    6. Geometry + CRS Safety
       - Validates geometry compatibility (Polygon → MultiPolygon, etc.)
       - Reprojects source CRS to match target SRID if needed

    7. Bulk Insert Compatible
       - Designed for GeoPandas `.to_postgis()` (COPY-based inserts)
       - Ensures compatibility with PostgreSQL COPY behavior

    ------------------------------------------------------------------------
    ⚠️ Important PostgreSQL Behavior
    ------------------------------------------------------------------------

    PostgreSQL applies DEFAULT values ONLY when a column is omitted.

        INSERT INTO table (col1) VALUES (...)
        → default applies to missing columns

        INSERT INTO table (col1, col2) VALUES (..., NULL)
        → default is NOT applied (NULL is used)

    This service ensures correct omission of columns to trigger defaults.

    ------------------------------------------------------------------------
    🧠 Column Rules Summary
    ------------------------------------------------------------------------

        db_nullable | has_default | behavior
        ------------------------------------------
        False       | False       → REQUIRED (must provide)
        False       | True        → OPTIONAL (omit if missing)
        True        | any         → OPTIONAL

    ------------------------------------------------------------------------
    🚀 Supported Features
    ------------------------------------------------------------------------

    - Auto column mapping (name + normalized matching)
    - CRS validation + reprojection
    - Geometry promotion (Polygon → MultiPolygon)
    - Protected column filtering (id, uuid, timestamps, etc.)
    - Default-aware column omission
    - Bulk append using GeoPandas

    ------------------------------------------------------------------------
    🧩 Intended Use
    ------------------------------------------------------------------------

    This service is designed to be reused across:
        - Multi-tenant GIS systems
        - Data ingestion pipelines
        - Admin upload tools
        - ETL workflows

    ------------------------------------------------------------------------
    """
```

---

# 📌 2. Usage Example (put in docs or README)

```python
service = DCHVectorAppendService(media_dir="/data/uploads", logger=logger)

# Step 1: Load shapefile
gdf = service.load_shapefile_to_gdf(shp_fp)

# Step 2: Inspect schemas
source_schema = service.get_source_schema(gdf)
target_schema = service.get_target_table_schema(engine, "wasa_area")

# Step 3: Auto mapping
mapping = service.suggest_column_mapping(
    source_columns=source_schema["columns"],
    target_columns=target_schema["column_names"],
    target_geometry_column=target_schema["geometry_column"],
)

# Step 4: Validate mapping
service.validate_final_mapping(
    final_mapping=mapping,
    source_schema=source_schema,
    target_schema=target_schema,
)

# Step 5: Prepare data
prepared_gdf = service.prepare_gdf_for_append(
    gdf=gdf,
    target_schema=target_schema,
    final_mapping=mapping,
)

# Step 6: Append to DB
rows_inserted = service.append_gdf_to_postgis(
    gdf=prepared_gdf,
    engine=engine,
    table_name="wasa_area",
)

print(f"Inserted {rows_inserted} rows")
```

---

# 📌 3. Developer Notes (VERY important — add near top or README)

```python
"""
Developer Notes:

1. DO NOT:
   - Add missing columns with None
   - Override DB defaults in code
   - Add table-specific logic here

2. If new behavior is needed:
   - Extend schema flags (input_required, has_default, etc.)
   - Keep logic generic

3. If you see NOT NULL errors:
   - Check if column is being sent as NULL instead of being omitted
   - Check normalization of empty strings ("", " ")

4. If DB default is not applied:
   - Ensure column is NOT included in GeoDataFrame before insert

5. Always ensure:
   - normalize_object_columns() runs BEFORE validation logic

6. For debugging:
   - Log final columns before to_postgis()
   - Check which columns were dropped

"""
```

---

# 🎯 Final takeaway (this is your architecture now)

You now have a system that is:

✅ Fully generic
✅ Works across all apps
✅ Respects DB design
✅ Avoids silent data corruption
✅ Handles GeoPandas + PostGIS correctly

---


