"""Copy these functions/imports into app/damapql/manage.py.
Adjust the MinioManager import to its actual location in your project.

=============================================================================
BACKUP EXAMPLES
=============================================================================

Backup complete bucket (as ZIP archive by default)
    python -m app.damapql.manage backup_minio --bucket lbdc-rasters --dest ./backups

Backup specific subfolder / prefix
    python -m app.damapql.manage backup_minio --bucket lbdc --prefix rasters/ --dest ./backups

Backup without compression (faster for already-compressed rasters/images)
    python -m app.damapql.manage backup_minio --bucket lbdc --dest ./backups --compress false

Backup to uncompressed folder directory instead of ZIP (legacy format)
    python -m app.damapql.manage backup_minio --bucket lbdc --dest ./backups --as_zip false

=============================================================================
RESTORE EXAMPLES (Supports both .zip archives and directory paths)
=============================================================================

Restore into the ORIGINAL / SAME bucket
    # Omitting --bucket automatically restores back into the bucket recorded in manifest.json:
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --mode overwrite

    # Or explicitly specify the same bucket name:
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --bucket lbdc --mode overwrite

    # Safe restore (only restores missing files, skips existing ones):
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --mode skip

Preview restore without uploading (Dry-Run)
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --mode dry-run

Restore into a DIFFERENT bucket
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --bucket lbdc-restored --mode overwrite --verify true

Restore selected subfolder / prefix only
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP.zip --prefix rasters/ --mode skip
"""

import os

from app.damapql.scripts.minio_backup import MinioBackupManager
from digitalarzengine.io.managers.minio_manager import MinioManager


def _minio_backup_service():
    endpoint = os.environ["MINIO_ENDPOINT"]
    secure = os.getenv("MINIO_SECURE", "false").lower() in ("true", "1", "yes")
    manager = MinioManager(
        endpoint=endpoint,
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        bucket_name=os.getenv("MINIO_BUCKET_NAME") or "backup-utility-placeholder",
        secure=secure,
        auto_create_bucket=False,
    )
    return MinioBackupManager(manager)


def backup_minio(bucket: str, dest: str = "./backups", prefix: str = "",
                 as_zip: str | bool = True, compress: str | bool = True):
    """
    Back up an entire MinIO bucket or one object prefix into a ZIP file.

    Example:
        python -m app.damapql.manage backup_minio --bucket lbdc-rasters --dest ./backups --as_zip true
    """
    is_zip = str(as_zip).lower() in ("true", "1", "yes")
    is_compress = str(compress).lower() in ("true", "1", "yes")
    return _minio_backup_service().backup(
        bucket_name=bucket,
        destination=dest,
        prefix=prefix,
        as_zip=is_zip,
        compress=is_compress
    )


def restore_minio(source: str, bucket: str = None, prefix: str = "",
                  mode: str = "skip", verify: str | bool = "true"):
    """
    Restore a MinIO backup from a .zip file or legacy directory.

    Example:
        python -m app.damapql.manage restore_minio --source ./backups/lbdc/20261021_221000_123456.zip --mode overwrite
    """
    return _minio_backup_service().restore(
        source=source,
        bucket_name=bucket,
        prefix=prefix,
        mode=mode,
        verify=str(verify).lower() in ("true", "1", "yes"),
    )


def clear_minio_bucket(
    bucket: str,
    confirm: str | bool = False,
    dry_run: str | bool = True,
):
    """
    Delete all current objects from a MinIO bucket.

    Examples:
        # Preview objects
        python -m app.damapql.manage clear_minio_bucket \
            --bucket lbdc-rasters

        # Permanently delete objects
        python -m app.damapql.manage clear_minio_bucket \
            --bucket lbdc-rasters \
            --confirm true \
            --dry_run false

    Notes:
        - Preserves the bucket itself.
        - Does not delete local ZIP backups.
        - Does not remove historical versions in versioned buckets.
    """

    is_confirmed = str(confirm).lower() in ("true", "1", "yes")
    is_dry_run = str(dry_run).lower() in ("true", "1", "yes")

    service = _minio_backup_service()
    client = service.client

    if not client.bucket_exists(bucket):
        raise ValueError(f"Bucket does not exist: {bucket}")

    objects = list(client.list_objects(bucket, recursive=True))
    count = len(objects)
    total_size = sum(obj.size or 0 for obj in objects)

    print(f"Bucket: {bucket}")
    print(f"Objects: {count}")
    print(f"Total size: {total_size / (1024 ** 2):.2f} MB")

    if is_dry_run:
        print("DRY RUN: No objects deleted.")
        return {
            "bucket": bucket,
            "selected": count,
            "deleted": 0,
            "dry_run": True,
        }

    if not is_confirmed:
        raise ValueError(
            "Deletion requires --confirm true --dry_run false"
        )

    from minio.deleteobjects import DeleteObject

    errors = list(
        client.remove_objects(
            bucket,
            (DeleteObject(obj.object_name) for obj in objects),
        )
    )

    if errors:
        for error in errors:
            print(f"DELETE ERROR: {error}")
        raise RuntimeError(
            f"Failed to delete all objects from bucket '{bucket}'"
        )

    remaining = list(client.list_objects(bucket, recursive=True))

    if remaining:
        raise RuntimeError(
            f"Bucket '{bucket}' still contains {len(remaining)} objects"
        )

    print(f"SUCCESS: Cleared bucket '{bucket}'")

    return {
        "bucket": bucket,
        "selected": count,
        "deleted": count,
        "remaining": 0,
        "dry_run": False,
    }


def minio_bucket_info(bucket: str = "lbdc-rasters") -> dict:
    """
    Display MinIO bucket statistics.

    Example:
        python -m app.damapql.manage minio_bucket_info \
            --bucket lbdc-rasters
    """

    service = _minio_backup_service()
    client = service.client

    if not client.bucket_exists(bucket):
        raise ValueError(f"MinIO bucket does not exist: {bucket}")

    total_objects = 0
    total_bytes = 0

    for obj in client.list_objects(bucket, recursive=True):
        if obj.object_name.endswith("/"):
            continue

        total_objects += 1
        total_bytes += obj.size or 0

    total_mb = round(total_bytes / (1024 ** 2), 2)
    total_gb = round(total_bytes / (1024 ** 3), 3)

    print("=" * 50)
    print("MINIO BUCKET INFORMATION")
    print("=" * 50)
    print(f"Bucket        : {bucket}")
    print(f"Total Objects : {total_objects:,}")
    print(f"Total Size    : {total_mb:,.2f} MB")
    print(f"Total Size    : {total_gb:,.3f} GB")
    print("=" * 50)

    return {
        "bucket": bucket,
        "total_objects": total_objects,
        "total_bytes": total_bytes,
        "total_mb": total_mb,
        "total_gb": total_gb,
    }