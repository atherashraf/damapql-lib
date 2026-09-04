"""
 * @file Admin Router Test Suite
 * @description Verifies administrative CRUD operations, role-based access control,
 * dynamic model mutations, PostGIS layer metadata reconciliation, and vector spatial workflows.
 *
 * ### Test Cases
 * - `test_get_db_connection_forbidden_for_normal_user` - Ensures non-admin users without proper privileges receive 403 Forbidden.
 * - `test_get_db_connection_success` - Verifies retrieval of configured external database connection IDs and names.
 * - `test_test_db_connection_success` - Confirms successful connectivity check against a target database using submitted credentials.
 * - `test_add_db_connection_success` - Validates encrypting credentials and registering a new database connection entry.
 * - `test_db_table_list_success` - Tests retrieving spatial table names from a configured external database connection.
 * - `test_save_db_layer_info_success` - Verifies automatic registration of an external spatial table into system layer metadata.
 * - `test_add_model_row_success` - Validates dynamic row creation in arbitrary models using form data payloads.
 * - `test_save_model_data_success` - Confirms dynamic field updates on model instances using composite or scalar primary keys.
 * - `test_add_layer_url_info_success` - Tests registering WMS/WFS/TMS external endpoints and application URL layers into metadata.
 * - `test_save_map_info_success` - Verifies saving map configuration records and normalizing serialized JSON layer references.
 * - `test_save_layer_info_normalizes_stringified_arrays_and_resolves_category` - Checks parsing stringified JSON extents and resolving category names to foreign keys.
 * - `test_preview_append_shp_success` - Validates shapefile upload analysis, schema compatibility check, and preview GeoJSON creation.
 * - `test_commit_append_shp_success` - Confirms transactional append of shapefile records into existing database tables and cache invalidation.
 * - `test_update_layer_attribute_success` - Tests updating a single feature attribute in an external spatial table via primary key matching.
 * - `test_bulk_update_layer_attribute_single_pk` - Verifies vectorized batch updates on a single column across multiple rows using a single PK.
 * - `test_bulk_update_layer_attribute_composite_pk` - Confirms parameterized batch SQL updates across multiple features using composite primary keys.
 * - `test_delete_layer_features_success` - Validates bulk deletion of spatial features from target database tables using primary key lists.
 * - `test_download_geometry_success` - Verifies querying selected row geometries and packaging them into a downloadable GeoPackage file.
 * - `test_upload_geometry_success` - Confirms single-feature geometry updates from uploaded GeoJSON, bound checks, and CRS reprojection.
 */
"""

import base64
import io
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point
from sqlalchemy import create_engine

from app.damapql.auth.dtypes import UserInfo
from app.damapql.auth.utils import AuthUtils
import app.damapql.dch.routers.admin as admin_module
from app.damapql.utils.adapters.deps import get_sync_session


# ---------------------------------------------------------------------
# User Fixtures
# ---------------------------------------------------------------------
@pytest.fixture
def admin_user():
    return UserInfo(
        id=1,
        username="admin_user",
        is_superuser=True,
        is_staff=True,
        roles=["app_manager"],
        department="gis",
        is_active=True,
        is_verified=True,
        name="Admin User",
        email="admin@example.com",
        contact=None,
    )


@pytest.fixture
def normal_user():
    return UserInfo(
        id=2,
        username="normal_user",
        is_superuser=False,
        is_staff=False,
        roles=["viewer"],
        department="operations",
        is_active=True,
        is_verified=True,
        name="Normal User",
        email="viewer@example.com",
        contact=None,
    )


@pytest.fixture(autouse=True)
def cleanup_and_mock_cache(monkeypatch, client):
    """
    Safely mock the MVT cache object so no missing-attribute exceptions occur.
    """
    fake_cache = MagicMock()
    fake_cache.clear_layer_cache.return_value = 1
    fake_cache.clear_layer.return_value = 1
    fake_cache.clear.return_value = 1

    monkeypatch.setattr(admin_module, "mvt_cache", fake_cache, raising=False)

    yield
    client.app.dependency_overrides.clear()

# ---------------------------------------------------------------------
# Helper for Session Context Manager Mocking
# ---------------------------------------------------------------------
def create_mock_session_with_rowcount(rowcount: int):
    """Creates a mock SQLAlchemy Session context manager returning an exact int rowcount."""
    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.rowcount = int(rowcount)
    mock_session.execute.return_value = mock_result
    mock_session.__enter__.return_value = mock_session
    mock_session.__exit__.return_value = False
    return mock_session

# ---------------------------------------------------------------------
# 1. DB Connection Endpoints Tests
# ---------------------------------------------------------------------
def test_get_db_connection_forbidden_for_normal_user(client, normal_user):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: normal_user
    res = client.get("/dch/admin/get_db_connection/")
    assert res.status_code in (401, 403)


def test_get_db_connection_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    @contextmanager
    def mock_managed_session():
        yield MagicMock()

    monkeypatch.setattr(admin_module.DBConnection, "managed_session", mock_managed_session)
    monkeypatch.setattr(
        admin_module.DBQuery,
        "execute_stmt_as_df",
        lambda session, stmt: pd.DataFrame([{"id": 1, "name": "local_postgres"}]),
    )

    res = client.get("/dch/admin/get_db_connection/")
    assert res.status_code == 200
    data = res.json()["payload"]
    assert len(data) == 1
    assert data[0]["name"] == "local_postgres"


def test_test_db_connection_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = mock_conn
    monkeypatch.setattr(admin_module, "create_engine", lambda url: mock_engine)

    payload = {
        "connection_name": "test_con",
        "engine": "postgresql",
        "params": {
            "host": "localhost",
            "port": "5432",  # Must be a string
            "name": "gis_db",
            "user": "postgres",
            "password": "secret_password",
        },
    }

    res = client.post("/dch/admin/test-db-connection/", json=payload)
    assert res.status_code == 200, res.text
    assert res.json()["payload"]["success"] is True


def test_add_db_connection_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    # Create dummy object where id and name are real primitive values
    class FakeConn:
        id = 10
        name = "gis_con"

    fake_conn = FakeConn()
    monkeypatch.setattr(admin_module.DBConnection, "get_db_connection", lambda **kwargs: fake_conn)

    payload = {
        "connection_name": "gis_con",
        "engine": "postgresql",
        "params": {
            "host": "localhost",
            "port": "5432",
            "name": "gis_db",
            "user": "postgres",
            "password": "secret_password",
        },
    }

    res = client.post("/dch/admin/add-db-connection/", json=payload)
    assert res.status_code == 200, res.text
    assert res.json()["payload"]["data"]["name"] == "gis_con"


def test_db_table_list_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    # Provide real in-memory engine so GeoDBManager doesn't attempt to quote mock passwords
    real_engine = create_engine("sqlite:///:memory:")
    fake_conn_record = MagicMock()
    fake_conn_record.get_sqlalchemy_engine.return_value = real_engine

    mock_db = MagicMock()
    mock_db.execute.return_value.scalar_one_or_none.return_value = fake_conn_record
    client.app.dependency_overrides[get_sync_session] = lambda: mock_db

    monkeypatch.setattr(
        admin_module.GeoDBManager,
        "get_spatial_table_names",
        lambda self: ["wasa_pipes", "wasa_valves"],
    )

    res = client.get("/dch/admin/db_table_list/1/")
    assert res.status_code == 200
    assert res.json()["payload"] == ["wasa_pipes", "wasa_valves"]


def test_save_db_layer_info_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    real_engine = create_engine("sqlite:///:memory:")
    fake_conn_record = MagicMock(name="local_con")
    fake_conn_record.get_sqlalchemy_engine.return_value = real_engine

    mock_db = MagicMock()
    mock_db.execute.return_value.scalar_one_or_none.return_value = fake_conn_record
    client.app.dependency_overrides[get_sync_session] = lambda: mock_db

    monkeypatch.setattr(
        admin_module.DCHUtils,
        "create_vector_layer_info",
        lambda **kwargs: True,
    )

    res = client.get("/dch/admin/save_db_layer_info/1/wasa_valves/3/")
    assert res.status_code == 200
    assert res.json()["payload"]["msg"] == "Layer info created"


# ---------------------------------------------------------------------
# 2. Dynamic Model Row Endpoints Tests
# ---------------------------------------------------------------------
def test_add_model_row_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user
    monkeypatch.setattr(admin_module.ModelUtils, "add_model_obj", lambda model_name, row: MagicMock())

    res = client.post(
        "/dch/admin/add_model_row/",
        data={
            "model_name": "LayerCategory",
            "main": "Water",
            "sub": "Hydrants",
        },
    )
    assert res.status_code == 200
    assert res.json()["payload"]["msg"] == "Row added successfully"


def test_save_model_data_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_model = MagicMock()
    mock_prop = MagicMock()
    mock_prop.property.columns = [MagicMock()]
    type(mock_prop.property.columns[0].type).__name__ = "VARCHAR"
    del mock_prop.property.columns[0].type.choices

    mock_model.manager.return_value.get_session.return_value = MagicMock()
    setattr(mock_model, "title", mock_prop)
    setattr(mock_model, "id", mock_prop)

    fake_row = MagicMock()
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = fake_row
    mock_model.manager.return_value.get_session.return_value = fake_session

    monkeypatch.setattr(admin_module.ModelUtils, "get_model", lambda name: mock_model)

    payload = {
        "pk": {"colName": "id", "colValue": 1},
        "rowData": {"title": "Updated Layer Title"},
    }

    res = client.post("/dch/admin/save_model_data/?model_name=LayerInfo", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["status"] == "success"


# ---------------------------------------------------------------------
# 3. Layer URL & Map Info Endpoints Tests
# ---------------------------------------------------------------------
def test_add_layer_url_info_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    @contextmanager
    def mock_session():
        session = MagicMock()
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)

    payload = {
        "layer_title": "Punjab Boundaries",
        "layer_category_id": 1,
        "layer_url": "https://geoserver.local/geoserver/wms",
        "url_type": "wms",
    }

    res = client.post("/dch/admin/add_layer_url_info/", json=payload)
    assert res.status_code == 200
    assert "added successfully" in res.json()["payload"]["msg"]


def test_save_map_info_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_map = MagicMock()

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = mock_map
        yield session

    monkeypatch.setattr(admin_module.MapInfo, "managed_session", mock_session)

    payload = {
        "pk": {"colName": "id", "colValue": 1},
        "rowData": {
            "title": "WASA Central Dashboard",
            "layers": '["layer-uuid-1", "layer-uuid-2"]',
        },
    }

    res = client.post("/dch/admin/save_map_info/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["status"] == "success"


# ---------------------------------------------------------------------
# 4. save_layer_info Tests
# ---------------------------------------------------------------------
def test_save_layer_info_normalizes_stringified_arrays_and_resolves_category(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.uuid = "4f8ee714-c180-414c-986f-97ba6377ac3a"
    mock_layer.srid = 3857
    mock_layer.extent_3857 = None

    for field in ["title", "extent", "extent_3857", "zoom_range", "geom_type", "category_id"]:
        setattr(mock_layer, field, None)

    fake_cat = MagicMock(id=5)

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.side_effect = [mock_layer, fake_cat]
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)

    payload = {
        "pk": {"colName": "uuid", "colValue": "4f8ee714-c180-414c-986f-97ba6377ac3a"},
        "rowData": {
            "title": "Fire Hydrant Fixed",
            "category": "Other",
            "extent": "[7717832.18, 3211585.40, 8389662.87, 4031923.97]",
            "extent_3857": "",
            "zoom_range": "[0, 30]",
            "geom_type": '["POINT"]',
        },
    }

    res = client.post("/dch/admin/save_layer_info/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["status"] == "success"

    assert isinstance(mock_layer.extent, list)
    assert isinstance(mock_layer.extent[0], float)
    assert mock_layer.zoom_range == [0, 30]
    assert mock_layer.geom_type == ["POINT"]


# ---------------------------------------------------------------------
# 5. Shapefile Preview & Commit Append Tests
# ---------------------------------------------------------------------
def test_preview_append_shp_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.data_model.code = admin_module.DataModel.V.name
    mock_layer.layer_accessibility.accessibility_type.code = admin_module.AccessibilityType.DB.name
    mock_layer.layer_accessibility.table_name = "wasa_sewers"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    monkeypatch.setattr(admin_module.LayerInfo, "get_layer_info_by_uuid", lambda uuid: mock_layer)

    # Async mock for save_files_from_form
    async def fake_save_files(*args, **kwargs):
        return ("upload-id-123", "/tmp/session", ["file.shp"])

    monkeypatch.setattr(
        admin_module.DCHUploadSessionService,
        "save_files_from_form",
        fake_save_files,
    )
    monkeypatch.setattr(
        admin_module.DCHUploadPackageService,
        "resolve_shapefile",
        lambda self, uid: {"main_file": "/tmp/session/test.shp", "base_name": "test"},
    )

    # Prepare GeoDataFrame with geom column
    fake_gdf = gpd.GeoDataFrame(
        {"id": [1], "name": ["Pipe 1"]},
        geometry=[Point(74.3, 31.5)],
        crs="EPSG:4326",
    )
    fake_gdf = fake_gdf.rename_geometry("geom")

    monkeypatch.setattr(admin_module.append_service, "load_shapefile_to_gdf", lambda path: fake_gdf)
    monkeypatch.setattr(admin_module.append_service, "get_target_table_schema", lambda **kwargs: {})
    monkeypatch.setattr(
        admin_module.append_service,
        "analyze_append_candidate",
        lambda gdf, schema: {"is_compatible": True, "fatal_errors": []},
    )

    res = client.post(
        "/dch/admin/preview_append_shp/",
        data={"layerUUID": "4f8ee714-c180-414c-986f-97ba6377ac3a"},
        files={"file": ("test.shp", b"fake binary content", "application/octet-stream")},
    )

    assert res.status_code == 200
    payload = res.json()["payload"]
    assert payload["layer_uuid"] == "4f8ee714-c180-414c-986f-97ba6377ac3a"
    assert payload["preview_feature_count"] == 1
    assert "preview_geojson_zip" in payload


def test_commit_append_shp_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.srid = 4326
    mock_layer.count_layer_rows.return_value = 10
    mock_layer.layer_accessibility.table_name = "wasa_valves"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    monkeypatch.setattr(admin_module.LayerInfo, "get_layer_info_by_uuid", lambda uuid: mock_layer)
    monkeypatch.setattr(
        admin_module.append_service,
        "get_shp_path_from_upload_id",
        lambda uid: ("/tmp/upload", "/tmp/upload/test.shp"),
    )

    # Assign .geom geometry column as expected by VectorAppendService
    fake_gdf = gpd.GeoDataFrame(
        {"diameter": [150]},
        geometry=[Point(74.35, 31.55)],
        crs="EPSG:4326",
    )
    fake_gdf = fake_gdf.rename_geometry("geom")

    monkeypatch.setattr(admin_module.append_service, "load_shapefile_to_gdf", lambda path: fake_gdf)
    monkeypatch.setattr(admin_module.append_service, "get_target_table_schema", lambda **kwargs: {})
    monkeypatch.setattr(admin_module.append_service, "validate_final_mapping", lambda *args: None)
    monkeypatch.setattr(admin_module.append_service, "prepare_gdf_for_append", lambda **kwargs: fake_gdf)
    monkeypatch.setattr(admin_module.append_service, "append_gdf_to_postgis", lambda **kwargs: 1)
    monkeypatch.setattr(admin_module.append_service, "cleanup_upload_dir", lambda p: None)

    form_data = {
        "upload_id": "session-xyz",
        "layer_uuid": "4f8ee714-c180-414c-986f-97ba6377ac3a",
        "final_mapping": json.dumps({"diameter": "diameter"}),
        "reproject_if_needed": "true",
    }

    res = client.post("/dch/admin/commit_append_shp/", data=form_data)
    assert res.status_code == 200
    assert res.json()["payload"]["appended_rows"] == 1


# ---------------------------------------------------------------------
# 6. Feature Attribute Updates & Deletions Tests
# ---------------------------------------------------------------------
def test_update_layer_attribute_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.layer_accessibility.cols_name = ["status", "pressure", "remarks"]
    mock_layer.layer_accessibility.pk_cols = ["id"]
    mock_layer.layer_accessibility.table_name = "wasa_valves"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = mock_layer
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)
    monkeypatch.setattr(admin_module, "Session", lambda engine: create_mock_session_with_rowcount(1))

    payload = {
        "pk_object": {"id": 101},
        "column": "status",
        "value": "Operational",
    }

    res = client.post("/dch/admin/update_layer_attributes/uuid-123/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["rows_affected"] == 1


def test_bulk_update_layer_attribute_single_pk(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.layer_accessibility.cols_name = ["status", "zone"]
    mock_layer.layer_accessibility.pk_cols = ["id"]
    mock_layer.layer_accessibility.table_name = "wasa_valves"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = mock_layer
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)
    monkeypatch.setattr(admin_module, "Session", lambda engine: create_mock_session_with_rowcount(3))

    payload = {
        "pk_objects": [{"id": 1}, {"id": 2}, {"id": 3}],
        "column": "status",
        "value": "Inspected",
    }

    res = client.post("/dch/admin/bulk_update_layer_field_value/uuid-123/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["rows_affected"] == 3
    assert res.json()["payload"]["pk_count"] == 3


def test_bulk_update_layer_attribute_composite_pk(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.layer_accessibility.cols_name = ["status", "zone"]
    mock_layer.layer_accessibility.pk_cols = ["code", "sub_code"]
    mock_layer.layer_accessibility.table_name = "wasa_assets"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = mock_layer
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)
    monkeypatch.setattr(admin_module, "Session", lambda engine: create_mock_session_with_rowcount(2))

    payload = {
        "pk_objects": [
            {"code": "A1", "sub_code": "01"},
            {"code": "B2", "sub_code": "02"},
        ],
        "column": "status",
        "value": "Archived",
    }

    res = client.post("/dch/admin/bulk_update_layer_field_value/uuid-composite/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["rows_affected"] == 2


def test_delete_layer_features_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.layer_accessibility.pk_cols = ["id"]
    mock_layer.layer_accessibility.table_name = "wasa_valves"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")

    @contextmanager
    def mock_session():
        session = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = mock_layer
        yield session

    monkeypatch.setattr(admin_module.LayerInfo, "managed_session", mock_session)
    monkeypatch.setattr(admin_module, "Session", lambda engine: create_mock_session_with_rowcount(2))

    payload = {"pk_objects": [{"id": 10}, {"id": 11}]}
    res = client.post("/dch/admin/delete_layer_features/uuid-123/", json=payload)
    assert res.status_code == 200
    assert res.json()["payload"]["rows_affected"] == 2





# ---------------------------------------------------------------------
# 7. Geometry Upload & Download Tests
# ---------------------------------------------------------------------

def test_upload_geometry_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_layer = MagicMock()
    mock_layer.data_model.code = admin_module.DataModel.V.name
    mock_layer.layer_accessibility.accessibility_type.code = admin_module.AccessibilityType.DB.name
    mock_layer.layer_accessibility.pk_cols = ["id"]
    mock_layer.layer_accessibility.table_name = "wasa_meters"
    mock_layer.layer_accessibility.geometry_col = "geom"
    mock_layer.layer_accessibility.params = {"schema": "public"}
    mock_layer.layer_accessibility.get_external_engine.return_value = create_engine("sqlite:///:memory:")
    mock_layer.srid = 4326

    monkeypatch.setattr(admin_module.LayerInfo, "get_layer_info_by_uuid", lambda uuid: mock_layer)
    monkeypatch.setattr(admin_module, "Session", lambda engine: create_mock_session_with_rowcount(1))

    valid_feature_geojson = json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [74.3587, 31.5204],
                },
                "properties": {},
            }
        ],
    })

    data = {
        "pk_object": json.dumps({"id": 55}),
        "source_srid": "4326",
    }
    files = {
        "file": ("feature.geojson", valid_feature_geojson.encode("utf-8"), "application/json"),
    }

    res = client.post("/dch/admin/upload_geometry/uuid-geom/", data=data, files=files)
    assert res.status_code == 200
    assert res.json()["payload"]["status"] == "success"
    assert res.json()["payload"]["rows_affected"] == 1

def test_download_geometry_success(client, admin_user, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: admin_user

    mock_geojson = MagicMock()
    mock_geojson.features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [74.3, 31.5]}}]
    fake_gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Point(74.3, 31.5)],
        crs="EPSG:4326",
    )
    mock_geojson.to_gdf.return_value = fake_gdf

    monkeypatch.setattr(
        admin_module.LayerQueryService,
        "get_features_geojson_by_pk_objects",
        lambda **kwargs: mock_geojson,
    )

    payload = {"pk_objects": [{"id": 1}]}
    res = client.post("/dch/admin/download_geometry/uuid-geom/", json=payload)
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/geopackage+sqlite3"

