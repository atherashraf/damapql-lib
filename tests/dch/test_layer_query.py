from app.damapql.auth.models import UserInfo
from app.damapql.auth.utils import AuthUtils
import app.damapql.dch.routers.layer_query as layer_query_module


def override_user():
    return UserInfo(
        id=1,
        username="manager",
        is_superuser=True,
        is_staff=True,
        roles=[],
        department="gis",
        is_active=True,
        is_verified=True,
        name="Manager",
        email="manager@example.com",
        contact=None,
    )


def test_get_layer_meta_by_layer_name(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_layer_meta(identifier, by):
        assert identifier == "district"
        assert by == "layer_name"
        return {
            "uuid": "abc-123",
            "layer_name": "district",
            "title": "Districts",
        }

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_layer_meta",
        fake_get_layer_meta,
    )

    response = client.get("/dch/layer-query/district/meta?by=layer_name")
    assert response.status_code == 200
    assert response.json()["payload"]["layer_name"] == "district"

    client.app.dependency_overrides.clear()


def test_get_layer_fields_by_uuid(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_layer_fields(identifier, by):
        assert identifier == "layer-1"
        assert by == "uuid"
        return [{"name": "id"}, {"name": "name"}]

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_layer_fields",
        fake_get_layer_fields,
    )

    response = client.get("/dch/layer-query/layer-1/fields")
    assert response.status_code == 200
    assert response.json()["payload"] == [{"name": "id"}, {"name": "name"}]

    client.app.dependency_overrides.clear()


def test_get_distinct_values_by_layer_name(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_field_distinct_values(identifier, by, field_name, where_clause, limit):
        assert identifier == "district"
        assert by == "layer_name"
        assert field_name == "dist_name"
        assert where_clause is None
        assert limit == 500
        return ["Lahore", "Kasur"]

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_field_distinct_values",
        fake_get_field_distinct_values,
    )

    response = client.get(
        "/dch/layer-query/district/distinct-values?by=layer_name&col_name=dist_name"
    )
    assert response.status_code == 200
    assert response.json()["payload"] == ["Lahore", "Kasur"]

    client.app.dependency_overrides.clear()


def test_get_distinct_values_with_where_clause(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_field_distinct_values(identifier, by, field_name, where_clause, limit):
        assert identifier == "tehsil"
        assert by == "layer_name"
        assert field_name == "tehsil_name"
        assert where_clause == "dist_id = 1"
        assert limit == 500
        return ["City", "Cantonment"]

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_field_distinct_values",
        fake_get_field_distinct_values,
    )

    response = client.get(
        "/dch/layer-query/tehsil/distinct-values"
        "?by=layer_name"
        "&col_name=tehsil_name"
        "&where_clause=dist_id%20%3D%201"
    )
    assert response.status_code == 200
    assert response.json()["payload"] == ["City", "Cantonment"]

    client.app.dependency_overrides.clear()


def test_get_layer_attributes_default_limit_all(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_layer_attributes(identifier, by, cols, where_clause, limit, offset):
        assert identifier == "district"
        assert by == "layer_name"
        assert cols is None
        assert where_clause is None
        assert limit == -1
        assert offset == 0
        return {
            "columns": ["id", "name"],
            "rows": [{"id": 1, "name": "Lahore"}],
            "total": 1,
            "limit": -1,
            "offset": 0,
            "pkCols": ["id"],
        }

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_layer_attributes",
        fake_get_layer_attributes,
    )

    response = client.get("/dch/layer-query/district/attributes?by=layer_name")
    assert response.status_code == 200

    payload = response.json()["payload"]
    assert payload["limit"] == -1
    assert payload["offset"] == 0
    assert payload["total"] == 1
    assert payload["rows"][0]["name"] == "Lahore"

    client.app.dependency_overrides.clear()


def test_get_layer_attributes_with_where_clause(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_layer_attributes(identifier, by, cols, where_clause, limit, offset):
        assert identifier == "tehsil"
        assert by == "layer_name"
        assert cols is None
        assert where_clause == "dist_id = 1"
        assert limit == -1
        assert offset == 0
        return {
            "columns": ["id", "tehsil_name", "dist_id"],
            "rows": [
                {"id": 1, "tehsil_name": "City", "dist_id": 1},
                {"id": 2, "tehsil_name": "Cantonment", "dist_id": 1},
            ],
            "total": 2,
            "limit": -1,
            "offset": 0,
            "pkCols": ["id"],
        }

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_layer_attributes",
        fake_get_layer_attributes,
    )

    response = client.get(
        "/dch/layer-query/tehsil/attributes"
        "?by=layer_name"
        "&where_clause=dist_id%20%3D%201"
    )
    assert response.status_code == 200

    payload = response.json()["payload"]
    assert payload["limit"] == -1
    assert payload["total"] == 2
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["dist_id"] == 1

    client.app.dependency_overrides.clear()


def test_get_layer_attributes_with_paging_cols_and_where_clause(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_layer_attributes(identifier, by, cols, where_clause, limit, offset):
        assert identifier == "public.tehsil"
        assert by == "table_name"
        assert cols == ["id", "tehsil_name"]
        assert where_clause == "dist_id = 1"
        assert limit == 100
        assert offset == 200
        return {
            "columns": ["id", "tehsil_name"],
            "rows": [{"id": 201, "tehsil_name": "Model Town"}],
            "total": 450,
            "limit": 100,
            "offset": 200,
            "pkCols": ["id"],
        }

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_layer_attributes",
        fake_get_layer_attributes,
    )

    response = client.get(
        "/dch/layer-query/public.tehsil/attributes"
        "?by=table_name"
        "&cols=id"
        "&cols=tehsil_name"
        "&where_clause=dist_id%20%3D%201"
        "&limit=100"
        "&offset=200"
    )
    assert response.status_code == 200

    payload = response.json()["payload"]
    assert payload["limit"] == 100
    assert payload["offset"] == 200
    assert payload["total"] == 450
    assert payload["rows"][0]["tehsil_name"] == "Model Town"

    client.app.dependency_overrides.clear()


def test_get_feature_detail(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_feature_detail(identifier, by, pk_values):
        assert identifier == "district"
        assert by == "layer_name"
        assert pk_values == ["1"]
        return {"id": 1, "dist_name": "Lahore"}

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_feature_detail",
        fake_get_feature_detail,
    )

    response = client.post(
        "/dch/layer-query/district/feature-detail?by=layer_name",
        json={"pk_values": ["1"]},
    )
    assert response.status_code == 200
    assert response.json()["payload"]["dist_name"] == "Lahore"

    client.app.dependency_overrides.clear()


def test_get_column_value(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_get_column_value(identifier, by, pk_values, col_name):
        assert identifier == "district"
        assert by == "layer_name"
        assert pk_values == ["1"]
        assert col_name == "status"
        return "Active"

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "get_column_value",
        fake_get_column_value,
    )

    response = client.post(
        "a/dch/layer-query/district/column-value?by=layer_name",
        json={"pk_values": ["1"], "col_name": "status"},
    )
    assert response.status_code == 200
    assert response.json()["payload"] == {"status": "Active"}

    client.app.dependency_overrides.clear()


def test_update_column_value(client, monkeypatch):
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    def fake_update_column_value(identifier, by, pk_values, col_name, value):
        assert identifier == "district"
        assert by == "layer_name"
        assert pk_values == ["1"]
        assert col_name == "status"
        assert value == "Inactive"
        return {
            "updated": 1,
            "column": "status",
            "value": "Inactive",
        }

    monkeypatch.setattr(
        layer_query_module.LayerQueryService,
        "update_column_value",
        fake_update_column_value,
    )

    response = client.post(
        "/dch/layer-query/district/update-column-value?by=layer_name",
        json={"pk_values": ["1"], "col_name": "status", "value": "Inactive"},
    )
    assert response.status_code == 200
    assert response.json()["payload"]["updated"] == 1
    assert response.json()["payload"]["value"] == "Inactive"

    client.app.dependency_overrides.clear()