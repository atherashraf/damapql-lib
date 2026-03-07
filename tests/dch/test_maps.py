from app.auth.dtypes import UserInfo
import app.dch.routers.maps as maps_module

# tests/dch/test_maps.py
from app.auth.utils import AuthUtils  # Ensure this is imported


def test_get_all_map_info(client, monkeypatch):
    def override_user():
        return UserInfo(
            id=1,
            username="manager",
            is_superuser=False,
            is_staff=True,
            roles=["app_manager"],
            department="gis",
            is_active=True,
            is_verified=True,
            name="Manager",
            email="manager@example.com",
            contact=None,
        )

    # 1. CORRECT: Use dependency_overrides for Auth
    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    # 2. FIX: Remove the monkeypatch for AuthUtils (redundant and causes 401s)

    # 3. KEEP: Monkeypatch the DB and Utility logic
    monkeypatch.setattr(
        maps_module.MapInfo,
        "get_all_map_info",
        lambda: __import__("pandas").DataFrame([{"uuid": "u1", "title": "Map 1"}]),
    )
    monkeypatch.setattr(
        maps_module.ManageDataFrameUtils,
        "grid_response",
        lambda df, pkCols=None: {"payload": df.to_dict(orient="records"), "pkCols": pkCols},
    )

    # 4. PATH: Standardize the URL (Removed trailing slash to match router)
    response = client.get("/dch/all_map_info")

    assert response.status_code == 200
    data = response.json()
    assert data["payload"][0]["uuid"] == "u1"

    client.app.dependency_overrides.clear()

def test_save_map_forbidden_for_non_superuser(client):
    from app.auth.utils import AuthUtils

    def override_user():
        return UserInfo(
            id=2,
            username="editor",
            is_superuser=False,
            is_staff=True,
            roles=["data_editor"],
            department="gis",
            is_active=True,
            is_verified=True,
            name="Editor",
            email="editor@example.com",
            contact=None,
        )

    client.app.dependency_overrides[AuthUtils.get_current_user] = override_user

    response = client.post(
        "/dch/save_map/",
        json={
            "uuid": "-1",
            "mapName": "Test Map",
            "extent": [1, 2, 3, 4],
            "baseLayer": "osm",
            "daLayers": [],
            "otherLayers": [],
        },
    )

    assert response.status_code == 403
    client.app.dependency_overrides.clear()