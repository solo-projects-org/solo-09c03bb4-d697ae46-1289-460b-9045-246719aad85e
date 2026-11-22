from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db as db_module  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test.db"
    db_module.set_db_path(str(db_path))
    with TestClient(app) as test_client:
        yield test_client
    db_module.set_db_path(db_module.DEFAULT_DB_PATH)


def make_road(client, road_id="G101", name="测试公路"):
    response = client.post("/roads", json={"id": road_id, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def make_version(client, road_id, segments, label=None, activate=False, expect=201):
    response = client.post(
        f"/roads/{road_id}/versions",
        json={
            "segments": [
                {"start_m": a, "end_m": b, "start_station": c} for a, b, c in segments
            ],
            "label": label,
            "activate": activate,
        },
    )
    assert response.status_code == expect, response.text
    return response.json()
