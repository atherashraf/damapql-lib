from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Generator

import pandas as pd
import pytest
from fastapi.testclient import TestClient

try:
    from app.damapql.auth.dtypes import UserInfo
except ImportError:
    from app.damapql.auth.models import UserInfo

from app.damapql.auth.utils import AuthUtils
import app.damapql.dch.routers.layers as layers_module
from digitalarzengine.utils.singletons import da_logger


# =============================================================================
# 1. FIXTURES & AUTH SETUP
# =============================================================================

@pytest.fixture
def manager_user() -> UserInfo:
    """Provides a consistent superuser/manager identity for auth bypass."""
    return UserInfo(
        id=1,
        username="manager",
        is_superuser=True,
        is_staff=True,
        roles=["app_manager"],
        department="gis",
        is_active=True,
        is_verified=True,
        name="Manager",
        email="manager@example.com",
        contact=None,
    )


@pytest.fixture
def authed_client(client: TestClient, manager_user: UserInfo) -> Generator[TestClient, None, None]:
    """TestClient fixture with dependency overrides automatically wired and torn down."""
    client.app.dependency_overrides[AuthUtils.get_current_user] = lambda: manager_user
    yield client
    client.app.dependency_overrides.clear()


class FakeSession:
    """Configurable context manager session double for SQLAlchemy execute queries."""
    def __init__(self, return_obj: Any):
        self.return_obj = return_obj

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.return_obj)


# =============================================================================
# 2. LAYER ENDPOINT TESTS
# =============================================================================

def test_get_layer_fields(authed_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Verifies that layer columns/fields are correctly extracted and returned."""
    expected_columns = ["id", "name", "geom"]
    fake_layer = SimpleNamespace(
        layer_accessibility=SimpleNamespace(params={"columns": expected_columns})
    )

    monkeypatch.setattr(
        layers_module.LayerInfo,
        "managed_session",
        lambda: FakeSession(fake_layer),
    )

    da_logger.info("Executing GET /dch/layer_fields/layer-1/ ...")
    response = authed_client.get("/dch/layer_fields/layer-1/")

    assert response.status_code == 200
    payload = response.json().get("payload")
    assert payload == expected_columns

    da_logger.success(
        {"Endpoint": "/dch/layer_fields/layer-1/", "Status": 200, "Columns": payload},
        extra={"test": "test_get_layer_fields"},
        as_table=True,
    )


def test_get_column_value_invalid_column(authed_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Verifies that requesting an invalid column raises an HTTP 400."""
    fake_info = SimpleNamespace(
        layer_accessibility=SimpleNamespace(
            cols_name=["id", "name"],
            pk_cols=["id"],
            table_name="public.test",
            get_external_engine=lambda: object(),
        ),
        pk_concat_char="-",
    )

    monkeypatch.setattr(
        layers_module.LayerInfo,
        "managed_session",
        lambda: FakeSession(fake_info),
    )

    da_logger.info("Executing GET /dch/column_value/layer-1/1/badcol/ (expecting 400)...")
    response = authed_client.get("/dch/column_value/layer-1/1/badcol/")

    assert response.status_code == 400
    da_logger.success(
        {"Endpoint": "/dch/column_value/layer-1/1/badcol/", "Status": 400, "Result": "Rejected Invalid Column"},
        extra={"test": "test_get_column_value_invalid_column"},
        as_table=True,
    )


def test_get_layer_field_distinct_values(authed_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Verifies that unique column values are extracted from the layer DataFrame."""
    raw_df = pd.DataFrame({"score": [1, 2, 2, 3]})
    fake_layer = SimpleNamespace(to_layer_df=lambda cols=None: raw_df)

    monkeypatch.setattr(
        layers_module.LayerInfo,
        "managed_session",
        lambda: FakeSession(fake_layer),
    )

    da_logger.info("Executing GET /dch/layer_field_distinct_values/layer-1/score/number/ ...")
    response = authed_client.get("/dch/layer_field_distinct_values/layer-1/score/number/")

    assert response.status_code == 200
    payload = response.json().get("payload")
    assert sorted(payload) == [1, 2, 3]

    da_logger.success(
        {"Field": "score", "Distinct Values": payload, "Status": 200},
        extra={"test": "test_get_layer_field_distinct_values"},
        as_table=True,
    )