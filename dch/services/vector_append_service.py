import os
import re
import json
import uuid
import shutil
from typing import Dict, List, Optional, Tuple, Any

import geopandas as gpd
from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


class DCHVectorAppendService:
    """
    Service class for shapefile-to-existing-table append workflow.
    """

    PROTECTED_COLUMNS = {
        "id",
        "fid",
        "objectid",
        "uuid",
        "created_at",
        "updated_at",
        "deleted_at",
    }

    COMMON_GEOM_NAMES = {"geom", "geometry", "the_geom", "wkb_geometry"}

    def __init__(self, media_dir: str, logger=None):
        self.media_dir = media_dir
        self.logger = logger
        self.temp_append_dir = os.path.join(media_dir, "upload_sessions")

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def log_info(self, msg: str) -> None:
        if self.logger:
            self.logger.info(msg)

    def log_exception(self, msg: str) -> None:
        if self.logger:
            self.logger.exception(msg)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def normalize_name(self, name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def get_upload_dir(self, upload_id: str) -> str:
        return os.path.join(self.temp_append_dir, upload_id)

    # ------------------------------------------------------------------
    # Upload / file handling
    # ------------------------------------------------------------------

    async def save_uploaded_shapefile_parts(self, request) -> Tuple[str, str, str]:
        form = await request.form()
        upload_files = [value for _, value in form.items() if hasattr(value, "filename")]

        if not upload_files:
            raise HTTPException(status_code=400, detail="No files provided")

        os.makedirs(self.temp_append_dir, exist_ok=True)

        upload_id = str(uuid.uuid4())
        file_dir = self.get_upload_dir(upload_id)
        os.makedirs(file_dir, exist_ok=True)

        shp_fp = None

        for file in upload_files:
            if not file.filename:
                continue

            file_path = os.path.join(file_dir, file.filename)

            with open(file_path, "wb") as f:
                shutil.copyfileobj(file.file, f)

            if file.filename.lower().endswith(".shp"):
                shp_fp = file_path

        if not shp_fp or not os.path.exists(shp_fp):
            raise HTTPException(status_code=400, detail="Missing .shp file in upload")

        self.validate_shapefile_parts(shp_fp, file_dir)

        return upload_id, file_dir, shp_fp

    def validate_shapefile_parts(self, shp_fp: str, file_dir: str) -> None:
        base_name = os.path.splitext(os.path.basename(shp_fp))[0]
        required_parts = [".shp", ".shx", ".dbf"]

        missing_parts = []
        for ext in required_parts:
            fp = os.path.join(file_dir, f"{base_name}{ext}")
            if not os.path.exists(fp):
                missing_parts.append(ext)

        if missing_parts:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required shapefile parts: {', '.join(missing_parts)}"
            )

    def get_shp_path_from_upload_id(self, upload_id: str) -> Tuple[str, str]:
        file_dir = self.get_upload_dir(upload_id)

        if not os.path.exists(file_dir):
            raise HTTPException(
                status_code=404,
                detail="Uploaded shapefile session not found or expired"
            )

        shp_files = [f for f in os.listdir(file_dir) if f.lower().endswith(".shp")]
        if not shp_files:
            raise HTTPException(status_code=400, detail="No .shp file found for this upload_id")

        shp_fp = os.path.join(file_dir, shp_files[0])
        return file_dir, shp_fp

    def cleanup_upload_dir(self, file_dir: str) -> None:
        if file_dir and os.path.exists(file_dir):
            shutil.rmtree(file_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Shapefile / GeoDataFrame helpers
    # ------------------------------------------------------------------

    def load_shapefile_to_gdf(self, shp_fp: str) -> gpd.GeoDataFrame:
        gdf = gpd.read_file(shp_fp)

        if gdf.empty:
            raise HTTPException(status_code=400, detail="Uploaded shapefile contains no features")

        gdf.columns = [c.lower() for c in gdf.columns]

        if gdf.geometry.name != "geom":
            gdf = gdf.rename_geometry("geom")

        cols_to_drop = [c for c in ["objectid", "fid"] if c in gdf.columns]
        if cols_to_drop:
            gdf = gdf.drop(columns=cols_to_drop)

        return gdf

    def get_source_schema(self, gdf: gpd.GeoDataFrame) -> Dict[str, Any]:
        source_columns = [c for c in gdf.columns if c != "geom"]

        geometry_type = None
        if len(gdf) > 0 and gdf.geom.notna().any():
            try:
                geometry_type = str(gdf.geom.geom_type.mode().iloc[0]).upper()
            except Exception:
                geometry_type = None

        source_epsg = None
        if gdf.crs:
            try:
                source_epsg = gdf.crs.to_epsg()
            except Exception:
                source_epsg = None

        return {
            "columns": source_columns,
            "geometry_column": "geom",
            "geometry_type": geometry_type,
            "crs": str(gdf.crs) if gdf.crs else None,
            "epsg": source_epsg,
        }

    # ------------------------------------------------------------------
    # Target schema helpers
    # ------------------------------------------------------------------

    def get_target_table_schema(
        self,
        engine: Engine,
        table_name: str,
        schema: str = "public"
    ) -> Dict[str, Any]:
        inspector = inspect(engine)

        if table_name not in inspector.get_table_names(schema=schema):
            raise HTTPException(
                status_code=404,
                detail=f"Target table '{schema}.{table_name}' not found"
            )

        columns = inspector.get_columns(table_name, schema=schema)

        table_cols = []
        geometry_column = None

        for col in columns:
            col_name = col["name"].lower()

            table_cols.append({
                "name": col_name,
                "type": str(col["type"]),
                "nullable": col.get("nullable", True),
                "default": str(col.get("default")) if col.get("default") is not None else None,
            })

            if col_name in self.COMMON_GEOM_NAMES:
                geometry_column = col_name

        geometry_meta_sql = text("""
            SELECT f_geometry_column, type, srid
            FROM geometry_columns
            WHERE f_table_schema = :schema
              AND f_table_name = :table_name
            LIMIT 1
        """)

        with engine.connect() as conn:
            row = conn.execute(
                geometry_meta_sql,
                {"schema": schema, "table_name": table_name}
            ).fetchone()

        geometry_type = None
        srid = None

        if row:
            geometry_column = row[0].lower()
            geometry_type = row[1].upper() if row[1] else None
            srid = int(row[2]) if row[2] is not None else None

        return {
            "schema": schema,
            "table": table_name,
            "columns": table_cols,
            "column_names": [c["name"] for c in table_cols],
            "geometry_column": geometry_column,
            "geometry_type": geometry_type,
            "srid": srid,
        }

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def suggest_column_mapping(
        self,
        source_columns: List[str],
        target_columns: List[str],
        target_geometry_column: Optional[str],
    ) -> Dict[str, str]:
        source_attr_cols = [c for c in source_columns if c not in self.PROTECTED_COLUMNS]
        target_attr_cols = [
            c for c in target_columns
            if c not in self.PROTECTED_COLUMNS and c != target_geometry_column
        ]

        mapping = {}
        used_targets = set()

        for src in source_attr_cols:
            if src in target_attr_cols and src not in used_targets:
                mapping[src] = src
                used_targets.add(src)

        normalized_target_map = {
            self.normalize_name(tgt): tgt
            for tgt in target_attr_cols
            if tgt not in used_targets
        }

        for src in source_attr_cols:
            if src in mapping:
                continue

            norm_src = self.normalize_name(src)
            tgt = normalized_target_map.get(norm_src)
            if tgt and tgt not in used_targets:
                mapping[src] = tgt
                used_targets.add(tgt)

        return mapping

    def get_missing_required_target_columns(
        self,
        target_schema: Dict[str, Any],
        final_mapping: Dict[str, str],
        extra_values: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        extra_values = {k.lower(): v for k, v in (extra_values or {}).items()}
        satisfied_targets = set(final_mapping.values()) | set(extra_values.keys())
        geom_col = target_schema["geometry_column"]

        missing_required = []

        for col in target_schema["columns"]:
            col_name = col["name"]

            if col_name == geom_col:
                continue

            if col_name in self.PROTECTED_COLUMNS:
                continue

            is_required = (col["nullable"] is False) and (col["default"] is None)

            if is_required and col_name not in satisfied_targets:
                missing_required.append(col_name)

        return missing_required

    def validate_final_mapping(
        self,
        final_mapping: Dict[str, str],
        source_schema: Dict[str, Any],
        target_schema: Dict[str, Any],
        extra_values: Optional[Dict[str, Any]] = None,
    ) -> None:
        extra_values = {k.lower(): v for k, v in (extra_values or {}).items()}

        source_columns = set(source_schema["columns"])
        target_columns = set(target_schema["column_names"])
        target_geom_col = target_schema["geometry_column"]

        seen_targets = set()

        for src, tgt in final_mapping.items():
            src = src.lower()
            tgt = tgt.lower()

            if src not in source_columns:
                raise HTTPException(status_code=400, detail=f"Invalid source column in mapping: {src}")

            if tgt not in target_columns:
                raise HTTPException(status_code=400, detail=f"Invalid target column in mapping: {tgt}")

            if tgt in self.PROTECTED_COLUMNS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Mapping to protected target column is not allowed: {tgt}"
                )

            if tgt == target_geom_col:
                raise HTTPException(
                    status_code=400,
                    detail="Geometry column should not be mapped as a normal attribute"
                )

            if tgt in seen_targets:
                raise HTTPException(
                    status_code=400,
                    detail=f"Multiple source columns mapped to same target column: {tgt}"
                )

            seen_targets.add(tgt)

        missing_required = self.get_missing_required_target_columns(
            target_schema=target_schema,
            final_mapping=final_mapping,
            extra_values=extra_values,
        )

        if missing_required:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required target mappings/values for: {', '.join(missing_required)}"
            )

    # ------------------------------------------------------------------
    # Geometry / CRS helpers
    # ------------------------------------------------------------------

    def geometry_compatibility(
        self,
        source_geom_type: Optional[str],
        target_geom_type: Optional[str],
    ) -> Dict[str, Any]:
        if not source_geom_type or not target_geom_type:
            return {
                "compatible": True,
                "warning": "Geometry type could not be fully determined",
                "promotion_needed": False,
            }

        source_geom_type = source_geom_type.upper()
        target_geom_type = target_geom_type.upper()

        if source_geom_type == target_geom_type:
            return {"compatible": True, "warning": None, "promotion_needed": False}

        if source_geom_type == "POLYGON" and target_geom_type == "MULTIPOLYGON":
            return {
                "compatible": True,
                "warning": "Polygon geometries will be promoted to MultiPolygon",
                "promotion_needed": True,
            }

        if source_geom_type == "LINESTRING" and target_geom_type == "MULTILINESTRING":
            return {
                "compatible": True,
                "warning": "LineString geometries will be promoted to MultiLineString",
                "promotion_needed": True,
            }

        if source_geom_type == "POINT" and target_geom_type == "MULTIPOINT":
            return {
                "compatible": True,
                "warning": "Point geometries will be promoted to MultiPoint",
                "promotion_needed": True,
            }

        return {
            "compatible": False,
            "warning": f"Incompatible geometry type: source={source_geom_type}, target={target_geom_type}",
            "promotion_needed": False,
        }

    def promote_geometry_if_needed(
        self,
        gdf: gpd.GeoDataFrame,
        target_geom_type: Optional[str],
    ) -> gpd.GeoDataFrame:
        if not target_geom_type:
            return gdf

        target_geom_type = target_geom_type.upper()

        try:
            from shapely.geometry import MultiPolygon, MultiLineString, MultiPoint
        except Exception:
            return gdf

        def _to_multi(geom):
            if geom is None:
                return geom

            if target_geom_type == "MULTIPOLYGON" and geom.geom_type == "Polygon":
                return MultiPolygon([geom])

            if target_geom_type == "MULTILINESTRING" and geom.geom_type == "LineString":
                return MultiLineString([geom])

            if target_geom_type == "MULTIPOINT" and geom.geom_type == "Point":
                return MultiPoint([geom])

            return geom

        gdf["geom"] = gdf["geom"].apply(_to_multi)
        return gdf

    # ------------------------------------------------------------------
    # Analysis / preview
    # ------------------------------------------------------------------

    def analyze_append_candidate(
        self,
        gdf: gpd.GeoDataFrame,
        target_schema: Dict[str, Any],
        extra_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        source_schema = self.get_source_schema(gdf)

        auto_mapping = self.suggest_column_mapping(
            source_columns=source_schema["columns"],
            target_columns=target_schema["column_names"],
            target_geometry_column=target_schema["geometry_column"],
        )

        unmatched_source_columns = [
            c for c in source_schema["columns"] if c not in auto_mapping
        ]

        mapped_target_columns = set(auto_mapping.values()) | set((extra_values or {}).keys())

        missing_target_columns = []
        for col in target_schema["columns"]:
            col_name = col["name"]

            if col_name == target_schema["geometry_column"]:
                continue
            if col_name in self.PROTECTED_COLUMNS:
                continue
            if col_name not in mapped_target_columns:
                missing_target_columns.append(col_name)

        geom_check = self.geometry_compatibility(
            source_schema["geometry_type"],
            target_schema["geometry_type"],
        )

        source_epsg = source_schema["epsg"]
        target_srid = target_schema["srid"]

        crs_match = source_epsg is not None and target_srid is not None and source_epsg == target_srid
        can_reproject = source_epsg is not None and target_srid is not None and source_epsg != target_srid

        missing_required_target_columns = self.get_missing_required_target_columns(
            target_schema=target_schema,
            final_mapping=auto_mapping,
            extra_values=extra_values,
        )

        warnings = []

        if not crs_match:
            if can_reproject:
                warnings.append(
                    f"Source CRS ({source_epsg}) differs from target SRID ({target_srid}); reprojection will be needed"
                )
            else:
                warnings.append("CRS could not be fully determined for source or target")

        if geom_check["warning"]:
            warnings.append(geom_check["warning"])

        if missing_required_target_columns:
            warnings.append(
                f"Required target columns still missing mapping/values: {', '.join(missing_required_target_columns)}"
            )

        return {
            "source": source_schema,
            "target": target_schema,
            "auto_mapping": auto_mapping,
            "unmatched_source_columns": unmatched_source_columns,
            "missing_target_columns": missing_target_columns,
            "missing_required_target_columns": missing_required_target_columns,
            "droppable_source_columns": unmatched_source_columns,
            "crs_match": crs_match,
            "can_reproject": can_reproject,
            "geometry_compatible": geom_check["compatible"],
            "geometry_promotion_needed": geom_check["promotion_needed"],
            "append_possible_after_mapping": geom_check["compatible"],
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Prepare data for append
    # ------------------------------------------------------------------

    def prepare_gdf_for_append(
        self,
        gdf: gpd.GeoDataFrame,
        final_mapping: Dict[str, str],
        target_schema: Dict[str, Any],
        reproject_if_needed: bool = True,
        extra_values: Optional[Dict[str, Any]] = None,
    ) -> gpd.GeoDataFrame:
        extra_values = {k.lower(): v for k, v in (extra_values or {}).items()}

        source_schema = self.get_source_schema(gdf)
        source_epsg = source_schema["epsg"]
        target_srid = target_schema["srid"]

        if target_srid is not None:
            if source_epsg is None:
                raise HTTPException(
                    status_code=400,
                    detail="Source shapefile CRS is undefined; cannot append safely"
                )

            if source_epsg != target_srid:
                if not reproject_if_needed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"CRS mismatch: source={source_epsg}, target={target_srid}"
                    )
                self.log_info(f"Reprojecting GDF from EPSG:{source_epsg} to EPSG:{target_srid}")
                gdf = gdf.to_crs(epsg=target_srid)

        geom_check = self.geometry_compatibility(
            self.get_source_schema(gdf)["geometry_type"],
            target_schema["geometry_type"],
        )

        if not geom_check["compatible"]:
            raise HTTPException(status_code=400, detail=geom_check["warning"])

        if geom_check["promotion_needed"]:
            gdf = self.promote_geometry_if_needed(gdf, target_schema["geometry_type"])

        gdf = gdf.rename(columns=final_mapping)

        for col_name, value in extra_values.items():
            gdf[col_name] = value

        target_geom_col = target_schema["geometry_column"]

        allowed_columns = {
            col["name"]
            for col in target_schema["columns"]
            if col["name"] not in self.PROTECTED_COLUMNS
        }
        allowed_columns.add("geom")

        gdf = gdf[[c for c in gdf.columns if c in allowed_columns]].copy()

        ordered_target_columns = []

        for col in target_schema["columns"]:
            col_name = col["name"]

            if col_name in self.PROTECTED_COLUMNS:
                continue

            if col_name == target_geom_col:
                ordered_target_columns.append("geom")
                continue

            is_required = (col["nullable"] is False) and (col["default"] is None)

            if col_name not in gdf.columns:
                if is_required:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Required target column '{col_name}' has no mapped or injected value"
                    )
                gdf[col_name] = None

            ordered_target_columns.append(col_name)

        gdf = gdf[[c for c in ordered_target_columns if c in gdf.columns]].copy()

        if gdf.geometry.name != "geom":
            gdf = gdf.set_geometry("geom")

        return gdf

    # ------------------------------------------------------------------
    # Append execution
    # ------------------------------------------------------------------

    def append_gdf_to_postgis(
            self,
            gdf: gpd.GeoDataFrame,
            engine: Engine,
            table_name: str,
            schema: str = "public",
    ) -> int:
        row_count = len(gdf)

        if row_count == 0:
            raise HTTPException(status_code=400, detail="No rows available to append")

        # --- Drop dch_id to allow PostGIS SERIAL to handle it ---
        if "dch_id" in gdf.columns:
            if self.logger:
                self.logger.info("Removing 'dch_id' from GeoDataFrame to allow database auto-increment.")
            gdf = gdf.drop(columns=["dch_id"])

        if self.logger:
            self.logger.info(f"Append columns: {list(gdf.columns)}")
            self.logger.info(f"Append dtypes: {gdf.dtypes.astype(str).to_dict()}")

        gdf.to_postgis(
            name=table_name,
            con=engine,
            schema=schema,
            if_exists="append",
            index=False,
        )

        return row_count