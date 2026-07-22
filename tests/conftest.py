from dotenv import load_dotenv


# Environment variables must be loaded before
# importing any application modules.
load_dotenv()


import pytest
from fastapi.testclient import TestClient

from app.damapql.auth.user_aoi import (
    BaseUserAOI,
    NullUserAOI,
)
from main import app


@pytest.fixture(autouse=True)
def use_null_user_aoi_provider(
    monkeypatch,
):
    """
    Keep DAMapQL auth unit tests independent from
    application-specific AOI models and databases.
    """
    monkeypatch.setattr(
        BaseUserAOI,
        "_implementation",
        NullUserAOI,
    )


@pytest.fixture
def client():
    """
    Create an API client using the application's
    configured /api root path.
    """
    with TestClient(
        app,
        base_url="http://testserver/api",
    ) as test_client:
        yield test_client