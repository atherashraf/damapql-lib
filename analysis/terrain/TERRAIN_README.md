# 🌊 Flood Susceptibility – Topographic & Hydrological Parameters

This document describes key **topographic and hydrological parameters** commonly used
in **flood susceptibility mapping**.  
Each parameter is explained in terms of:

1. **What it is**
2. **Why it matters for floods**
3. **How it is derived**

---

## 1. Digital Elevation Model (DEM)

### What it is
A raster representation of bare-earth elevation above mean sea level.

### Why it matters for floods
- Controls gravity-driven water movement
- Determines flow direction, accumulation, and floodplain extent
- Low-lying areas are inherently more flood-prone

### How it is derived
- Obtained from satellite missions (MERIT, SRTM, ALOS)
- Preprocessed for noise removal and consistent resolution
- Base layer for all hydrological analysis

---

## 2. Filled DEM (Hydrologically Corrected DEM)

### What it is
DEM after removal of artificial sinks and depressions.

### Why it matters for floods
- Ensures continuous downslope flow
- Prevents false water trapping
- Essential for realistic flow routing

### How it is derived
- Sink-filling algorithms (e.g., Wang & Liu)
- Raises pits to lowest spill elevation while preserving terrain shape

---

## 3. Slope

### What it is
Rate of elevation change in the steepest downslope direction.

### Why it matters for floods
- Controls runoff velocity
- Low slope → ponding and inundation
- High slope → rapid runoff and erosion

### How it is derived
- First derivative of DEM
- Expressed in degrees, radians, or percent rise

---

## 4. Aspect

### What it is
Direction of maximum slope (0–360°, clockwise from North).

### Why it matters for floods
- Influences soil moisture, evapotranspiration, and snowmelt
- Affects spatial distribution of wetness

### How it is derived
- Computed from DEM slope gradients
- Flat areas usually assigned no-data

---

## 5. Profile Curvature

### What it is
Curvature in the direction of maximum slope.

### Why it matters for floods
- Controls flow acceleration or deceleration
- Negative → accelerating flow
- Positive → decelerating flow

### How it is derived
- Second derivative of DEM along slope direction

---

## 6. Plan Curvature

### What it is
Curvature perpendicular to the slope direction.

### Why it matters for floods
- Indicates flow convergence or divergence
- Convergent areas accumulate water
- Divergent areas disperse flow

### How it is derived
- Second derivative of DEM across contour lines

---

## 7. Flow Direction (D8)

### What it is
Direction of steepest descent from each cell to one of its eight neighbors.

### Why it matters for floods
- Determines water routing paths
- Foundation for flow accumulation and drainage networks

### How it is derived
- D8 algorithm
- Encoded as directional values (1–128)

---

## 8. Flow Accumulation

### What it is
Number of upstream cells contributing flow to a given cell.

### Why it matters for floods
- Proxy for channel initiation
- High accumulation → rivers and floodplains

### How it is derived
- Accumulating flow paths using the flow direction grid

---

## 9. Specific Contributing Area (SCA)

### What it is
Upslope contributing area per unit contour width (m²/m).

### Why it matters for floods
- Represents potential water supply to a location
- Used in hydrological indices

### How it is derived
- Flow accumulation multiplied by cell area
- Normalized by contour width

---

## 10. Topographic Wetness Index (TWI)

### What it is
\[
TWI = \ln\left(\frac{SCA}{\tan(\beta)}\right)
\]

### Why it matters for floods
- Indicates soil saturation potential
- High TWI → saturated soils → higher flood risk

### How it is derived
- Combines specific contributing area and slope (radians)
- Log transformation improves stability

---

## 11. Stream Power Index (SPI)

### What it is
\[
SPI = SCA \times \tan(\beta)
\]

### Why it matters for floods
- Measures flow energy and erosive potential
- High SPI → active channels and flood damage zones

### How it is derived
- Multiplication of contributing area and slope gradient

---

## 12. Terrain Ruggedness Index (TRI)

### What it is
Mean elevation difference between a cell and its neighbors.

### Why it matters for floods
- Low TRI → flat, flood-prone areas
- High TRI → rapid runoff and lower inundation

### How it is derived
- Neighborhood analysis on DEM elevation differences

---

## 13. Relative Relief

### What it is
Local elevation range within a moving window.

### Why it matters for floods
- Low relief → floodplains
- High relief → well-drained terrain

### How it is derived
- Sliding window calculation:
  - Relief = max elevation − min elevation

---

## 14. Valley Depth

### What it is
Vertical distance from terrain surface to nearest valley bottom.

### Why it matters for floods
- Low valley depth → high flood susceptibility
- High valley depth → elevated terrain

### How it is derived
- Uses hydrologically corrected DEM
- Identifies valley bottoms and computes vertical distance

---

## 15. Convergence Index

### What it is
Measure of terrain convergence or divergence.

### Why it matters for floods
- Convergent areas concentrate runoff
- Strong predictor of flood pathways

### How it is derived
- Based on angular differences in surrounding slopes
- Positive → convergent
- Negative → divergent

---

## 16. Topographic Position Index (TPI)

### What it is
\[
TPI = z_{cell} - \overline{z}_{neighborhood}
\]

### Why it matters for floods
- Negative TPI → valleys (flood-prone)
- Positive TPI → ridges (safer zones)

### How it is derived
- Difference between cell elevation and neighborhood mean
- Window size controls analysis scale

---

## 17. Height Above Nearest Drainage (HAND)

### What it is
Vertical distance above nearest stream channel.

### Why it matters for floods
- One of the strongest predictors of flood inundation
- Low HAND → frequent flooding
- High HAND → flood-safe terrain

### How it is derived
- Requires DEM and drainage network
- Computes elevation difference relative to stream cells

---

## Summary

These parameters collectively describe:
- **Terrain shape**
- **Water routing**
- **Accumulation potential**
- **Floodplain geometry**

They are widely used in:
- Flood susceptibility mapping
- Hydrological modeling
- Machine learning–based flood prediction

---
