

import pytest
from fastapi.testclient import TestClient
from dotenv import load_dotenv

load_dotenv()  # Load vars BEFORE importing the app
from main import app


@pytest.fixture
def client():
    # Setting base_url tells TestClient to treat /api as the root
    return TestClient(app, base_url="http://testserver/api")