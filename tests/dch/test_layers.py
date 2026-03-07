from types import SimpleNamespace
import pandas as pd

from app.auth.dtypes import UserInfo
from app.auth.utils import AuthUtils
import app.dch.routers.layers as layers_module


def test_get_layer_fields(client, monkeypatch):
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

    fake_layer = SimpleNamespace(
        layer_accessibility=SimpleNamespace(params={"columns": ["id", "name", "geom"]})
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: fake_layer)

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user
    monkeypatch.setattr(layers_module.LayerInfo, "managed_session", lambda: FakeSession())

    response = client.get("/dch/layer_fields/layer-1/")
    assert response.status_code == 200
    assert response.json()["payload"] == ["id", "name", "geom"]

    client.app.dependency_overrides.clear()


def test_get_column_value_invalid_column(client, monkeypatch):
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

    fake_info = SimpleNamespace(
        layer_accessibility=SimpleNamespace(
            cols_name=["id", "name"],
            pk_cols=["id"],
            table_name="public.test",
            get_external_engine=lambda: object(),
        ),
        pk_concat_char="-",
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: fake_info)

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user
    monkeypatch.setattr(layers_module.LayerInfo, "managed_session", lambda: FakeSession())

    response = client.get("/dch/column_value/layer-1/1/badcol/")
    assert response.status_code == 400

    client.app.dependency_overrides.clear()


def test_get_layer_field_distinct_values(client, monkeypatch):
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

    fake_layer = SimpleNamespace(
        to_layer_df=lambda cols=None: pd.DataFrame({"score": [1, 2, 2, 3]})
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: fake_layer)

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user
    monkeypatch.setattr(layers_module.LayerInfo, "managed_session", lambda: FakeSession())

    response = client.get("/dch/layer_field_distinct_values/layer-1/score/number/")
    assert response.status_code == 200
    assert sorted(response.json()["payload"]) == [1, 2, 3]

    client.app.dependency_overrides.clear()