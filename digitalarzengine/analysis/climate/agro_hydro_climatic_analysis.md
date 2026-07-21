# AgroHydroClimaticAnalysis Framework

The `AgroHydroClimaticAnalysis` framework is an integrated agro-hydro-climatic analysis system designed to evaluate evapotranspiration dynamics, climatic water balance, agricultural stress, irrigation dependency, and atmospheric water demand.

The framework combines:

* MODIS MOD16A2GF evapotranspiration products
* ERA5-Land climatic variables
* Seasonal and annual aggregation
* Hydro-climatic stress indicators
* Agricultural water stress metrics

The framework is particularly suitable for:

* Agro-Ecological Zone (AEZ) delineation
* Irrigation planning
* Water resource assessment
* Agricultural sustainability analysis
* Climate resilience studies
* Drought and aridity assessment

The implementation integrates:

* climate,
* hydrology,
* and agricultural water demand

within a unified geospatial workflow. 

---

# Data Sources

## MODIS MOD16A2GF

Used for:

* Actual Evapotranspiration (ET)
* Potential Evapotranspiration (PET)
* Latent Heat Flux (LE)

### Advantages

* 500 m spatial resolution
* vegetation-aware evapotranspiration estimation
* physically based Penman-Monteith methodology
* suitable for agricultural spatial analysis

---

## ERA5-Land

Used for:

* precipitation
* temperature
* solar radiation
* wind speed
* vapor pressure deficit

### Advantages

* physically consistent atmospheric reanalysis
* long-term temporal consistency
* monthly climatic aggregation
* suitable for climatic driver analysis

---

# Seasonal Structure

The framework supports:

| Season | Months  |
| ------ | ------- |
| Winter | Dec–Feb |
| Spring | Mar–May |
| Summer | Jun–Aug |
| Autumn | Sep–Nov |
| Annual | Jan–Dec |

Seasonal and annual aggregations are implemented using Earth Engine temporal filtering. 

---

# Core Hydro-Climatic Variables

## 1. Actual Evapotranspiration (ET)

Actual evapotranspiration represents the real atmospheric water loss from soil and vegetation surfaces.

### Equation

ET

### Unit

* Annual: mm/year
* Seasonal: mm/season

### Dataset

MODIS MOD16A2GF

### Aggregation

Seasonal and annual sum.

---

## 2. Potential Evapotranspiration (PET)

Potential evapotranspiration represents the atmospheric evaporative demand under unlimited water availability.

### Equation

PET

### Unit

* Annual: mm/year
* Seasonal: mm/season

### Dataset

MODIS MOD16A2GF

### Aggregation

Seasonal and annual sum.

---

## 3. Precipitation (P)

Total precipitation derived from ERA5-Land monthly precipitation sums.

### Equation

P

### Unit

* Annual: mm/year
* Seasonal: mm/season

### Dataset

ERA5-Land

### Conversion

ERA5 precipitation is provided in meters and converted to millimeters:

P_{mm}=P_{m}\times1000

---

# Atmospheric Demand Variables

## 4. Temperature

Mean 2 m air temperature.

### Conversion

ERA5 temperature is converted from Kelvin to Celsius:

T_{^\circ C}=T_K-273.15

### Unit

°C

---

## 5. Wind Speed

Wind speed is derived from the u and v wind components.

### Equation

WS=\sqrt{u^2+v^2}

### Unit

m/s

---

## 6. Solar Radiation

Surface net solar radiation.

### Conversion

ERA5 radiation is converted from Joules to Megajoules:

MJ=\frac{J}{10^6}

### Unit

MJ/m²

---

## 7. Vapor Pressure Deficit (VPD)

VPD measures atmospheric dryness and evaporative demand.

### Saturation Vapor Pressure

e_s=0.6108\exp\left(\frac{17.27T}{T+237.3}\right)

### Actual Vapor Pressure

e_a=0.6108\exp\left(\frac{17.27T_d}{T_d+237.3}\right)

### Vapor Pressure Deficit

VPD=e_s-e_a

### Unit

kPa

---

# Derived Hydro-Climatic Indicators

## 8. ET/PET Ratio

Measures how closely actual evapotranspiration approaches atmospheric demand.

### Equation

\frac{ET}{PET}

### Interpretation

| Value | Interpretation                 |
| ----- | ------------------------------ |
| High  | adequate moisture availability |
| Low   | water limitation               |

### Unit

Dimensionless

---

## 9. Water Deficit

Represents unmet atmospheric evaporative demand.

### Equation

PET-ET

### Interpretation

Higher values indicate stronger moisture stress.

### Unit

* mm/year
* mm/season

---

## 10. Evaporative Stress Index (ESI)

Indicates evapotranspiration stress relative to atmospheric demand.

### Equation

ESI=1-\frac{ET}{PET}

### Interpretation

| Value | Interpretation |
| ----- | -------------- |
| Low   | low stress     |
| High  | severe stress  |

### Unit

Dimensionless

---

## 11. Moisture Availability Index (MAI)

Represents climatic moisture adequacy.

### Equation

MAI=\frac{P}{PET}

### Interpretation

| Value | Interpretation   |
| ----- | ---------------- |
| > 1   | moisture surplus |
| < 1   | moisture deficit |

### Unit

Dimensionless

---

## 12. Climatic Water Balance (CWB)

Represents climatic water surplus or deficit.

### Equation

CWB=P-PET

### Interpretation

| Value    | Interpretation |
| -------- | -------------- |
| Positive | surplus        |
| Negative | deficit        |

### Unit

* mm/year
* mm/season

---

## 13. Irrigation Dependency (ID)

Represents irrigation requirement caused by insufficient rainfall relative to atmospheric demand.

### Equation

ID=\max(PET-P,0)

### Interpretation

Higher values indicate stronger irrigation dependency.

### Unit

* mm/year
* mm/season

---

# Agricultural Stress Indicator

## 14. Crop Water Stress Proxy

Composite agricultural stress indicator integrating:

* evaporative stress,
* atmospheric dryness,
* thermal stress.

### Equation

CWS=0.5(ESI)+0.3(VPD)+0.2(T)

### Output Range

| Range | Interpretation |
| ----- | -------------- |
| 0     | low stress     |
| 1     | severe stress  |

### Unit

Dimensionless index

---

# Spatial Analysis Workflow

The framework supports:

* raster extraction
* seasonal raster stacking
* zonal statistics
* governorate-level aggregation
* spatial distribution analysis

using:

* Earth Engine,
* rasterio-based processing,
* and zonal statistical analysis. 

---

# Scientific Importance

The framework enables identification of:

* climatically stressed agricultural zones
* irrigation-dependent regions
* high atmospheric demand areas
* moisture-limited landscapes
* agricultural sustainability risk zones
* drought-prone environments

The integration of:

* evapotranspiration,
* climatic demand,
* precipitation,
* and agricultural stress

provides a robust basis for:

* agro-ecological zoning,
* climate resilience assessment,
* and sustainable agricultural planning.
