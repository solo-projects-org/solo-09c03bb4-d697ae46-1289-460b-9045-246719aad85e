"""并发与持久化约束测试。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import make_road, make_version

OLD_SEGMENTS = [(0, 1000, 0), (1000, 2000, 0)]


def test_segments_cannot_be_updated_or_deleted(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS)
    store = client.app.state.store

    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as conn:
            conn.execute(
                "UPDATE chain_segments SET start_station = 999 WHERE chain_id = 'G101' AND version_no = 1"
            )

    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as conn:
            conn.execute("DELETE FROM chain_segments WHERE chain_id = 'G101' AND version_no = 1")

    # 触发器拒绝后数据保持原样，连接仍可用。
    version = client.get("/roads/G101/versions/1").json()
    assert [s["start_station"] for s in version["segments"]] == [0.0, 0.0]


def test_version_number_cannot_be_changed(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS)
    store = client.app.state.store
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as conn:
            conn.execute(
                "UPDATE chain_versions SET version_no = 9 WHERE chain_id = 'G101' AND version_no = 1"
            )


def test_confirmed_span_cannot_be_changed_in_db(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    client.post(
        "/roads/G101/damages",
        json={
            "damage_no": "D-001",
            "old_version": 1,
            "start_station": 1000,
            "end_station": 500,
            "measured_length": 500,
        },
    )
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})

    store = client.app.state.store
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as conn:
            conn.execute(
                "UPDATE damages SET confirmed_start_m = 0 WHERE chain_id = 'G101' AND damage_no = 'D-001'"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store.tx() as conn:
            conn.execute(
                "UPDATE damages SET status = 'needs_selection', confirmed_version = NULL "
                "WHERE chain_id = 'G101' AND damage_no = 'D-001'"
            )

    body = client.get("/roads/G101/damages/D-001").json()
    assert body["status"] == "confirmed"
    assert body["confirmed"]["start_m"] == 1000.0


def test_parallel_version_registration_gets_unique_numbers(client):
    make_road(client)

    def register(index):
        return make_version(client, "G101", [(0, 1000 + index, 0)])["version_no"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        numbers = list(pool.map(register, range(8)))

    assert sorted(numbers) == list(range(1, 9))


def test_only_one_active_version(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    make_version(client, "G101", [(0, 2000, 0)], activate=True)
    versions = client.get("/roads/G101/versions").json()
    assert [v["status"] for v in versions] == ["superseded", "active"]


def test_state_survives_new_store(client, tmp_path):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    client.post(
        "/roads/G101/damages",
        json={
            "damage_no": "D-001",
            "old_version": 1,
            "start_station": 1000,
            "end_station": 500,
            "measured_length": 500,
        },
    )
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 500, "end_m": 1000})

    # 重新打开同一个库文件，确认记录与版本仍在。
    from app.db import Store
    from app.service import ChainService

    reopened = ChainService(Store(client.app.state.store.path))
    damage = reopened.get_damage("G101", "D-001")
    assert damage["status"] == "confirmed"
    assert damage["confirmed"]["start_m"] == 500
    assert reopened.current_version("G101")["version_no"] == 1
