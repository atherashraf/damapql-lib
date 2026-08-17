"""
DCH Batch Raster Ingestion Utility

Harvests rasters from a local directory or storage path, uploads them to MinIO
with clean S3 object prefixes (e.g. 'pid-idss/Surfaces/ETact'), auto-resolves
Main and Sub LayerCategories, and registers LayerInfo/LayerAccessibility.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import unicodedata
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.damapql.auth.models import User
from app.damapql.dch.models import LayerCategory, LayerInfo
from app.damapql.dch.services.ingestion.raster_ingest_service import RasterIngestService
from app.damapql.settings import (MINIO_ACCESS_KEY, MINIO_BUCKET_NAME, MINIO_ENDPOINT, MINIO_SECRET_KEY, MINIO_SECURE, )
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger

SUPPORTED_RASTER_EXTENSIONS = {".tif", ".tiff", ".cog", ".jp2", ".img", ".vrt"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _slugify(text: str) -> str:
    """Matches the LayerCategory.slug property calculation."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def resolve_category_id(session: Session, category_id: Optional[int] = None, object_prefix: Optional[str] = None,
        folder_path: Optional[Path] = None, auto_create: bool = True, ) -> int:
    """
    Resolves category ID explicitly or parses main/sub hierarchy from object_prefix
    (e.g., 'pid-idss/Surfaces/ETact' -> main='Surfaces', sub='ETact').
    """
    if category_id is not None:
        cat = session.get(LayerCategory, int(category_id))
        if cat:
            return cat.id

    main_val: Optional[str] = None
    sub_val: Optional[str] = None

    # 1. Parse main/sub from object_prefix (e.g. 'pid-idss/Surfaces/ETact')
    if object_prefix:
        parts = [p.strip() for p in object_prefix.strip("/\\").replace("\\", "/").split("/") if p.strip()]
        if len(parts) >= 2:
            main_val = parts[-2]
            sub_val = parts[-1]
        elif len(parts) == 1:
            main_val = parts[0]
            sub_val = None

    # 2. Fallback: Parse from folder directory path structure
    if not main_val and folder_path:
        sub_val = folder_path.parent.name
        main_val = folder_path.parent.parent.name if folder_path.parent.parent.name else "Other"

    # Query matching (main, sub) pair
    if main_val:
        query = session.query(LayerCategory).filter(LayerCategory.main.ilike(main_val))
        if sub_val:
            query = query.filter(LayerCategory.sub.ilike(sub_val))
        else:
            query = query.filter((LayerCategory.sub.is_(None)) | (LayerCategory.sub == ""))

        category = query.first()
        if category:
            return category.id

        # Query matching slug property in-memory
        target_slug = _slugify(f"{main_val}-{sub_val}" if sub_val else main_val)
        all_categories = session.query(LayerCategory).all()
        for cat in all_categories:
            if cat.slug == target_slug:
                return cat.id

        # Auto-create if not found
        if auto_create:
            new_cat = LayerCategory(main=main_val, sub=sub_val)
            session.add(new_cat)
            session.flush()
            return new_cat.id

    # 3. Fallback to 'Other'
    fallback = session.execute(select(LayerCategory).where(LayerCategory.main.ilike("Other"))).scalar_one_or_none()
    if fallback:
        return fallback.id

    # 4. Fallback to first category in DB
    first_cat = session.query(LayerCategory).first()
    if first_cat:
        return first_cat.id

    if auto_create:
        default_cat = LayerCategory(main="Other", sub=None)
        session.add(default_cat)
        session.flush()
        return default_cat.id

    raise RuntimeError("No LayerCategory records exist in the database and auto_create is disabled.")


def upload_to_minio_if_needed(minio_manager: MinioManager, bucket_name: str, object_name: str,
        local_file_path: Path, ) -> None:
    """Uploads local raster file to MinIO if not already uploaded."""
    if hasattr(minio_manager.client, "bucket_exists"):
        if not minio_manager.client.bucket_exists(bucket_name):
            minio_manager.client.make_bucket(bucket_name)

    exists = False
    try:
        exists = minio_manager.object_exists(bucket_name, object_name)
    except Exception:
        exists = False

    if not exists:
        print(f"   ⬆️ Uploading {local_file_path.name} to s3://{bucket_name}/{object_name}...")
        if hasattr(minio_manager, "upload_file"):
            minio_manager.upload_file(bucket_name, object_name, str(local_file_path))
        else:
            minio_manager.client.fput_object(bucket_name, object_name, str(local_file_path))

import re
from datetime import datetime, date
from pathlib import Path


def extract_date_from_filename(file_path: str, is_str: bool = True, ) -> str | date | None:
    """
    Extract a YYYYMMDD date from any part of a filename.

    Example:
        et_24_mm_20260330.tif

    Returns:
        is_str=True  -> "2026-03-30"
        is_str=False -> date(2026, 3, 30)
        no date      -> None
    """
    filename = Path(file_path).name

    for match in re.findall(r"(?<!\d)\d{8}(?!\d)", filename):
        try:
            dt = datetime.strptime(match, "%Y%m%d").date()

            return dt.isoformat() if is_str else dt

        except ValueError:
            continue

    return None

def ingest_raster_folder(folder: Optional[str] = None, f: Optional[str] = None, category_id: Optional[int] = None,
        temporal: Optional[str] = None, model_type: str = "Surface", use_minio: bool = False, minio: bool = False,
        object_prefix: Optional[str] = None, username: Optional[str] = None, recursive: bool = True,
        bucket_name: str = MINIO_BUCKET_NAME, ):
    target_folder = folder or f
    if not target_folder:
        print("❌ Target folder path is required via --folder or -f")
        return

    use_minio = _to_bool(use_minio) or _to_bool(minio)
    recursive = _to_bool(recursive)
    target_dir = Path(target_folder).resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        da_logger.error(f"Target folder not found or invalid: {target_dir}")
        print(f"❌ Target directory does not exist: {target_dir}")
        return

    minio_manager = None
    if use_minio:
        da_logger.info(f"Initializing MinIO client against {MINIO_ENDPOINT}...")
        minio_manager = MinioManager(endpoint=MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE, )

    pattern = "**/*" if recursive else "*"
    raster_files = [p for p in target_dir.glob(pattern) if
        p.is_file() and p.suffix.lower() in SUPPORTED_RASTER_EXTENSIONS]

    if not raster_files:
        print(f"⚠️ No matching raster files found in {target_dir}")
        return

    # Normalize prefix (e.g. 'pid-idss/Surfaces/ETact')
    prefix = object_prefix.strip("/\\").replace("\\", "/") if object_prefix else target_dir.name

    da_logger.info(f"Discovered {len(raster_files)} raster file(s) for batch ingestion.")
    print(f"\n=======================================================")
    print(f"  DCH BATCH RASTER INGESTION")
    print(f"=======================================================")
    print(f"  Directory     : {target_dir}")
    print(f"  Target Mode   : {'MinIO (' + bucket_name + ')' if use_minio else 'Local Persistent Media'}")
    if use_minio:
        print(f"  Object Prefix : {prefix}")
    print(f"  Total Files   : {len(raster_files)}")
    print(f"=======================================================\n")

    uploader_id = None
    if username:
        with User.managed_session() as auth_session:
            user = auth_session.query(User).filter(User.username == username.strip().lower()).first()
            if user:
                uploader_id = user.id

    started_at = time.time()
    success_records: List[Dict[str, Any]] = []
    failed_records: List[Dict[str, Any]] = []

    for idx, file_path in enumerate(raster_files, start=1):
        file_name = file_path.name
        title = file_path.stem.replace("_", " ").replace("-", " ")

        print(f"[{idx}/{len(raster_files)}] Ingesting: {file_name} ...")

        with LayerInfo.managed_session() as session:
            try:
                resolved_cat_id = resolve_category_id(session=session, category_id=category_id,
                    object_prefix=prefix if use_minio else None, folder_path=file_path, auto_create=True, )

                # if use_minio and minio_manager:
                #     # try:
                #     #     rel_path = file_path.relative_to(target_dir).as_posix()
                #     # except ValueError:
                #     #     rel_path = file_path.name
                #
                #     clean_object_name = f"{prefix}/{file_path.name}"
                #
                #     # # 1. Upload local file to MinIO
                #     # upload_to_minio_if_needed(minio_manager=minio_manager, bucket_name=bucket_name,
                #     #     object_name=clean_object_name, local_file_path=file_path, )
                #
                #     # 2. Pass S3 object key to ingest service
                #     target_raster_path = clean_object_name
                # else:
                #     target_raster_path = str(file_path)


                temporal = extract_date_from_filename(file_path.name) if temporal is None else temporal
                result = RasterIngestService.ingest_from_path(session=session, minio_manager=minio_manager, title=title,
                    category_id=resolved_cat_id, raster_path=str(file_path),
                    temporal=temporal or date.today().isoformat(), model_type=model_type,
                    uploaded_by=uploader_id, bucket_name=bucket_name, )

                success_records.append({"File": file_name, "UUID": result.uuid, "LayerInfo ID": result.layer_info_id,
                    "Access ID": result.layer_access_id, "Status": "Success", })
                print(f"   ✅ Success (UUID: {result.uuid})\n")

            except Exception as exc:
                session.rollback()
                failed_records.append({"File": file_name, "Error": str(exc), "Status": "Failed", })
                da_logger.error(f"Failed to ingest raster {file_name}: {exc}")
                print(f"   ❌ Ingestion Failed: {exc}\n")

    elapsed = time.time() - started_at

    print("\n=======================================================")
    print("  INGESTION SUMMARY")
    print("=======================================================")
    print(f"  Total Processed : {len(raster_files)}")
    print(f"  Successful      : {len(success_records)}")
    print(f"  Failed          : {len(failed_records)}")
    print(f"  Elapsed Time    : {elapsed:.2f} seconds")
    print("=======================================================\n")

    if success_records:
        da_logger.info(f"Ingested {len(success_records)} rasters successfully.")

    if failed_records:
        da_logger.warning(f"Failed to ingest {len(failed_records)} rasters.")
