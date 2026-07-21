# 🌧️ Rainfall Analysis using CHIRPS & Google Earth Engine

## Overview

This module provides a **consistent, quality-controlled rainfall analysis framework**
based on **daily CHIRPS precipitation data**, processed through **Google Earth Engine (GEE)**
and summarized using **Python (pandas)**.

It is designed for **arid and semi-arid regions** (e.g., Madinah Governorate), where trace
rainfall and data completeness must be handled carefully.

The outputs are suitable for:
- Climate reporting
- Hydrological assessment
- Abnormal Rainfall Days (ARD)
- Planning and risk communication

---

## Data Source

- **Dataset**: CHIRPS – Daily precipitation (mm/day)
- **Platform**: Google Earth Engine
- **Temporal Resolution**: Daily
- **Spatial Resolution**: ~5.5 km (scale = 5500 m)
- **Study Period**: User-defined (e.g., 2016–2025)

---

## Key Design Principles

### 1. Trace Rainfall Handling (Critical)

Rainfall values **below a configurable threshold** (default: `0.3 mm/day`)
are treated as **trace/noise** and set to zero **before aggregation**.

```python
TRACE_ZERO_MM = 0.3
```

This ensures:
- Consistent annual and monthly totals
- Correct dry/wet day counts
- Reliable statistics in arid climates

---

### 2. Consistency Across All Aggregations

The same trace threshold is applied everywhere:
- Annual totals
- Monthly climatology
- Monthly × Year matrices
- Dry/Wet day counts

This avoids discrepancies between summary tables.

---

### 3. AOI-Based Statistics

All rainfall values represent **area-averaged precipitation** over a defined AOI:
- Governorate
- District
- Watershed
- Any polygon supported by the GEE pipeline

---

## Module Structure

### `RainfallAnalysis` Class

Handles:
- Data extraction from GEE
- Quality control
- Aggregation and climatology
- Excel-ready pandas outputs

---

## Core Functionalities

### 1. Daily Rainfall Extraction

```python
get_daily_precipitation(start_date, end_date)

```

Returns a DataFrame with:
- date
- precipitation_mean
- precipitation_p10
- precipitation_p90
- precipitation_max

---

### 2. Average Annual Total Rainfall (GEE-side)

```python
average_annual_total_rainfall(start_year, end_year)

```

**Methodology**
1. Apply trace threshold daily
2. Compute AOI mean rainfall per day
3. Sum daily means → annual total
4. Average across years

---

### 3. Data Quality Control

```python
quality_screen_daily(start_date, end_date)
```

Checks:
- Expected vs available days
- Missing percentage
- Confidence flag (OK / LOW)

---

### 4. Annual Rainfall Totals

```python
annual_rainfall_totals(df, value_col)
```

Returns yearly effective rainfall totals (mm/year).

---

### 5. Monthly Climatology

```python
monthly_climatology_named(df, value_col)
```

Mean monthly rainfall totals across all years.

---

### 6. Monthly × Year Rainfall Matrix

```python
monthly_year_matrix(df, value_col)
```

Matrix useful for:
- Excel reporting
- Heatmaps
- Trend analysis

---

### 7. Monthly Dry / Wet Day Counts

```python
monthly_daycount_matrix(df, value_col, day_type="dry")
monthly_daycount_matrix(df, value_col, day_type="wet")
```

- Dry day: P < threshold
- Wet day: P ≥ threshold

---

## Typical Workflow

```python
rf_analysis = RainfallAnalysis("UCSB-CHG/CHIRPS/DAILY", scale=5500)

df = rf_analysis.get_daily_precipitation("2016-01-01", "2025-12-31")

annual = rf_analysis.annual_rainfall_totals(df, "precipitation_mean")
monthly = rf_analysis.monthly_climatology_named(df, "precipitation_mean")
matrix = rf_analysis.monthly_year_matrix(df, "precipitation_mean")
dry_days = rf_analysis.monthly_daycount_matrix(df, "precipitation_mean", day_type="dry")
wet_days = rf_analysis.monthly_daycount_matrix(df, "precipitation_mean", day_type="wet")
```

---

## Outputs (Excel-Ready)

Designed for exporting a single Excel workbook with:
- Meta
- Annual
- Monthly_Climatology
- Monthly_Year_Matrix
- Monthly_Dry_Days
- Monthly_Wet_Days

---

## Intended Use Cases

- Abnormal Rainfall Days (ARD)
- Climate diagnostics for arid regions
- Water resources & flood preparedness
- Government and municipal reporting
- Research-grade climatology pipelines

---

## Notes & Best Practices

- Always report the trace threshold used
- Prefer area-mean rainfall for administrative reporting
- Flag LOW confidence when missing data > 2%
- In arid climates, trace handling is essential
