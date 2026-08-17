from __future__ import annotations

import socket
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from fastapi import HTTPException, UploadFile
from rasterio.transform import from_origin

import app.damapql.dch.services.ingestion.raster_ingest_service as raster_ingest_module
from app.damapql.dch.services.ingestion.raster_ingest_service import RasterIngestService
from app.damapql.settings import (MINIO_ACCESS_KEY, MINIO_BUCKET_NAME, MINIO_ENDPOINT, MINIO_SECRET_KEY, MINIO_SECURE, )
from digitalarzengine.io.cog_raster import COGRaster
from digitalarzengine.io.managers.minio_manager import MinioManager
from digitalarzengine.utils.singletons import da_logger


# =============================================================================
# 1. NETWORK & MINIO CONNECTIVITY UTILS
# =============================================================================

def is_minio_server_reachable() -> bool:
    """Checks if the actual MinIO host/port is accepting TCP socket connections."""
    try:
        endpoint = MINIO_ENDPOINT.replace("http://", "").replace("https://", "").rstrip("/")
        host, port = endpoint.split(":") if ":" in endpoint else (endpoint, 9000)
        da_logger.info(f"Probing MinIO connectivity at {host}:{port}...")
        with socket.create_connection((host, int(port)), timeout=1.5):
            da_logger.success({"Status": "Connected", "Endpoint": f"{host}:{port}", "Secure": MINIO_SECURE},
                extra={"service": "MinIO"}, as_table=True, )
            return True
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        da_logger.warning(f"MinIO health probe failed at {MINIO_ENDPOINT}: {e}",
            extra={"endpoint": MINIO_ENDPOINT, "status": "OFFLINE"}, )
        return False


# =============================================================================
# 2. FIXTURES & DATA DOUBLES
# =============================================================================

@pytest.fixture
def fake_category() -> SimpleNamespace:
    return SimpleNamespace(id=1, slug="test-raster", main="test", sub="raster", )


@pytest.fixture
def fake_session(fake_category: SimpleNamespace) -> object:
    """SQLAlchemy Session test double supporting add, flush, get, commit, and rollback."""

    class FakeSession:
        def __init__(self):
            self.committed = False
            self.rolled_back = False
            self._next_id = 1000
            self.added_objects = []

        def get(self, model, pk):
            return fake_category if pk == fake_category.id else None

        def add(self, obj):
            self.added_objects.append(obj)
            da_logger.debug(f"FakeSession.add called for {type(obj).__name__}")

        def flush(self):
            for obj in self.added_objects:
                if getattr(obj, "id", None) is None:
                    self._next_id += 1
                    setattr(obj, "id", self._next_id)
            da_logger.debug("FakeSession.flush processed added entities.")

        def commit(self):
            self.committed = True
            da_logger.info("FakeSession transaction committed.")

        def rollback(self):
            self.rolled_back = True
            da_logger.warning("FakeSession transaction rolled back.")

    return FakeSession()


@pytest.fixture
def real_minio_manager() -> MinioManager:
    """Returns a real, live MinioManager configured from application settings."""
    da_logger.info(f"Initializing MinioManager for endpoint: {MINIO_ENDPOINT}")
    return MinioManager(endpoint=MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE, )


@pytest.fixture
def fake_raster_path(tmp_path) -> str:
    """Creates a real valid normal GeoTIFF (non-COG) with EPSG:4326."""
    raster_path = tmp_path / "sample_raster.tif"
    width, height = 100, 100

    data = np.arange(width * height, dtype=np.float32).reshape(height, width)
    transform = from_origin(74.0, 32.0, 0.001, 0.001)

    with rasterio.open(raster_path, "w", driver="GTiff", width=width, height=height, count=1, dtype=data.dtype,
            crs="EPSG:4326", transform=transform, nodata=-9999.0, ) as dst:
        dst.write(data, 1)
        dst.set_band_description(1, "Test Band")

    da_logger.debug({"File": str(raster_path), "Dimensions": f"{width}x{height}", "CRS": "EPSG:4326"},
        extra={"stage": "fixture_generation"}, as_table=True, )
    return raster_path


@pytest.fixture
def unreferenced_raster_path(tmp_path) -> str:
    """Creates a real raster missing CRS (unreferenced) to test spatial validation."""
    raster_path = tmp_path / "nocrs_raster.tif"
    data = np.ones((50, 50), dtype=np.float32)

    with rasterio.open(raster_path, "w", driver="GTiff", width=50, height=50, count=1, dtype=data.dtype, crs=None,
            transform=from_origin(0, 0, 1, 1), ) as dst:
        dst.write(data, 1)

    da_logger.warning(f"Generated unreferenced GeoTIFF (No CRS) at: {raster_path}")
    return raster_path


# =============================================================================
# 3. DIRECT LOCAL INGESTION TESTS (NO MOCKS)
# =============================================================================

def test_ingest_upload_local_real_execution(tmp_path, monkeypatch: pytest.MonkeyPatch, fake_session,
        fake_raster_path, ):
    """Executes full upload pipeline to local disk using real file system and GDAL tools."""
    media_root = tmp_path / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(raster_ingest_module, "MEDIA_BASE", str(media_root))

    da_logger.info("Starting direct local upload ingestion pipeline...")

    try:
        with open(fake_raster_path, "rb") as file_handle:
            upload_file = UploadFile(filename="uploaded_sample.tif", file=file_handle)

            result = RasterIngestService.ingest_upload(session=fake_session, minio_manager=None,
                title="Real Local Raster", category_id=1, temporal="2026-08-17", model_type="surface",
                raster_file=upload_file, uploaded_by=99, )

        assert result.uuid is not None
        assert fake_session.committed is True
        assert fake_session.rolled_back is False

        persistent_dir = media_root / "test-raster"
        assert persistent_dir.exists()
        persistent_files = list(persistent_dir.glob("*.tif"))
        assert len(persistent_files) == 1
        assert COGRaster.is_cog(str(persistent_files[0]))

        da_logger.success(
            {"UUID": result.uuid, "LayerInfo ID": result.layer_info_id, "Access ID": result.layer_access_id,
                "Persistent Path": str(persistent_files[0]), "Is COG": True, }, extra={"status": "INGESTED_LOCAL"},
            as_table=True, )

    except Exception as exc:
        da_logger.exception(f"Direct local ingestion failed: {exc}", extra={"file": str(fake_raster_path)})
        raise


def test_ingest_from_path_missing_crs_raises_http_400(fake_session, unreferenced_raster_path, ):
    """Feeds a real GeoTIFF with no CRS to ensure validation raises an HTTP 400."""
    da_logger.info(f"Testing validation rejection on missing CRS raster: {unreferenced_raster_path}")

    with pytest.raises(HTTPException) as exc_info:
        try:
            RasterIngestService.ingest_from_path(session=fake_session, minio_manager=None, title="No CRS Raster",
                category_id=1, raster_path=str(unreferenced_raster_path), temporal="2026-08-17", model_type="test",
                uploaded_by=99, )
        except HTTPException as exc:
            da_logger.warning(f"Ingestion rejected unreferenced raster: HTTP {exc.status_code} - {exc.detail}",
                extra={"path": str(unreferenced_raster_path)}, )
            raise
        except Exception as exc:
            da_logger.exception(f"Unexpected non-HTTP error during CRS check: {exc}")
            raise

    assert exc_info.value.status_code == 400
    assert "Raster CRS is missing" in exc_info.value.detail
    assert fake_session.rolled_back is True
    da_logger.success("Missing CRS validation assertion passed.", as_table=False)


# =============================================================================
# 4. REAL MINIO INGESTION & ERROR DETECTION (NO MOCKS)
# =============================================================================

def test_ingest_upload_minio_execution_and_offline_diagnostics(fake_session, fake_raster_path, real_minio_manager, ):
    """Directly tests MinIO upload.

    - When offline: captures socket/connection exceptions, displays stack trace, verifies rollback.
    - When online: runs live upload and persists into the real target bucket.
    """
    if not is_minio_server_reachable():
        da_logger.warning(f"MinIO is OFFLINE at {MINIO_ENDPOINT}. Expecting connection failure during upload pipeline.",
            extra={"target_bucket": MINIO_BUCKET_NAME}, )

        with open(fake_raster_path, "rb") as fh:
            upload_file = UploadFile(filename="minio_sample.tif", file=fh)
            with pytest.raises(Exception) as exc_info:
                try:
                    RasterIngestService.ingest_upload(session=fake_session, minio_manager=real_minio_manager,
                        title="MinIO Offline Test", category_id=1, temporal="2026-08-17", model_type="Surface",
                        raster_file=upload_file, uploaded_by=99, bucket_name=MINIO_BUCKET_NAME, )
                except Exception as exc:
                    da_logger.exception(f"Expected offline error caught during real MinIO upload: {type(exc).__name__}",
                        extra={"endpoint": MINIO_ENDPOINT, "bucket": MINIO_BUCKET_NAME}, )
                    raise

            assert fake_session.rolled_back is True
            da_logger.success("Offline rollback verified successfully.", as_table=False)

    else:
        da_logger.info(f"MinIO is ONLINE. Executing live upload to bucket: '{MINIO_BUCKET_NAME}'...")
        try:
            with open(fake_raster_path, "rb") as fh:
                upload_file = UploadFile(filename="minio_live_sample.tif", file=fh)
                result = RasterIngestService.ingest_upload(session=fake_session, minio_manager=real_minio_manager,
                    title="MinIO Live Raster", category_id=1, temporal="2026-08-17", mode_type="Surface",
                    raster_file=upload_file, uploaded_by=99, bucket_name=MINIO_BUCKET_NAME, )

                assert result.uuid is not None
                assert fake_session.committed is True
                da_logger.success(
                    {"UUID": result.uuid, "MinIO Bucket": MINIO_BUCKET_NAME, "Status": "Uploaded & Registered", },
                    extra={"storage": "MinIO Live"}, as_table=True, )
        except Exception as exc:
            da_logger.exception(f"Live MinIO upload failed unexpectedly: {exc}")
            raise


def test_ingest_from_path_minio_nonexistent_object(fake_session, real_minio_manager, ):
    """Attempts to register a non-existent object directly against live MinIO."""
    if not is_minio_server_reachable():
        da_logger.warning(f"MinIO server offline at {MINIO_ENDPOINT}; skipping remote object existence check.",
            extra={"status": "SKIPPED"}, )
        pytest.skip(f"MinIO server offline at {MINIO_ENDPOINT}; skipping remote object check.")

    missing_s3_path = f"s3://{MINIO_BUCKET_NAME}/non_existent_folder/missing_file.tif"
    da_logger.info(f"Checking missing remote object handling against: {missing_s3_path}")

    with pytest.raises(HTTPException) as exc_info:
        try:
            RasterIngestService.ingest_from_path(session=fake_session, minio_manager=real_minio_manager,
                title="Non-existent MinIO Raster", category_id=1, raster_path=missing_s3_path, temporal="2026-08-17",
                model_type="surface", uploaded_by=99, bucket_name=MINIO_BUCKET_NAME, )
        except HTTPException as exc:
            da_logger.warning(f"MinIO check returned expected HTTP status: {exc.status_code} - {exc.detail}",
                extra={"path": missing_s3_path}, )
            raise
        except Exception as exc:
            da_logger.exception(f"Unexpected error querying missing remote object: {exc}")
            raise

    assert exc_info.value.status_code == 404
    assert "Raster not found in MinIO" in exc_info.value.detail
    assert fake_session.rolled_back is True
    da_logger.success("MinIO missing object 404 validation confirmed.", as_table=False)
