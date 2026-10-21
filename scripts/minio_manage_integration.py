"""Copy these functions/imports into app/damapql/manage.py.
Adjust the MinioManager import to its actual location in your project.
Backup complete bucket
    python -m app.damapql.manage backup_minio --bucket lbdc-rasters --dest ./backups
Backup specific subfolder
    python -m app.damapql.manage backup_minio --bucket lbdc --prefix rasters/ --dest ./backups

Preview restore
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP --mode dry-run

Restore into another bucket
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP --bucket lbdc-restored --mode overwrite --verify true

Restore selected subfolder
    python -m app.damapql.manage restore_minio --source ./backups/lbdc/TIMESTAMP --prefix rasters/ --mode skip

"""
import os

from app.damapql.scripts.minio_backup import MinioBackupManager
from digitalarzengine.io.managers.minio_manager import MinioManager

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
