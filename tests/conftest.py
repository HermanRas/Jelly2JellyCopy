import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Set before app.config is imported: the suite must never touch a real /data.
os.environ.setdefault("J2J_DATA_DIR", tempfile.mkdtemp(prefix="j2j-test-"))
os.environ.pop("J2J_APP_PASSWORD", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", config.DEFAULT_APP_PASSWORD)
    auth._failures.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def logged_in(client):
    r = client.post("/login", data={"password": config.APP_PASSWORD})
    assert r.status_code == 303
    return client
