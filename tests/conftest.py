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

from app import auth, config, db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", config.DEFAULT_APP_PASSWORD)
    auth._failures.clear()
    db.db_path().unlink(missing_ok=True)
    db.init()
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def logged_in(client):
    r = client.post("/login", data={"password": config.APP_PASSWORD})
    assert r.status_code == 303
    return client
