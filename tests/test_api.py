"""接口层测试：登记、定位、确认、供数、换版覆盖校验。"""

from __future__ import annotations

from conftest import make_road, make_version

# 旧表（版本 1）：两段实距相接，桩号全部重号（都从 K0 起算）。
OLD_SEGMENTS = [
    (0, 1000, 0),
    (1000, 2000, 0),
]


def locate(
    client,
    road_id="G101",
    damage_no="D-001",
    old_version=1,
    start_station=1000,
    end_station=500,
    measured_length=500,
):
    return client.post(
        f"/roads/{road_id}/damages",
        json={
            "damage_no": damage_no,
            "old_version": old_version,
            "start_station": start_station,
            "end_station": end_station,
            "measured_length": measured_length,
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_road_crud(client):
    make_road(client)
    assert client.get("/roads").json()[0]["id"] == "G101"
    assert client.get("/roads/G101").json()["name"] == "测试公路"
    assert client.post("/roads", json={"id": "G101", "name": "重复"}).status_code == 409
    assert client.get("/roads/NOPE").status_code == 404


def test_version_registration_is_fixed(client):
    make_road(client)
    version = make_version(client, "G101", OLD_SEGMENTS, label="初版")
    assert version["version_no"] == 1
    assert version["status"] == "draft"
    assert [s["seq"] for s in version["segments"]] == [1, 2]

    # 内容登记后固定：重复登记只产生新版本，旧版不变。
    again = make_version(client, "G101", OLD_SEGMENTS)
    assert again["version_no"] == 2
    assert client.get("/roads/G101/versions/1").json()["segments"] == version["segments"]


def test_version_rejects_overlapping_segments(client):
    make_road(client)
    make_version(client, "G101", [(0, 1000, 0), (900, 2000, 0)], expect=422)
    assert client.get("/roads/G101/versions").json() == []


def test_locate_returns_multiple_candidates(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    response = locate(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "needs_selection"
    assert body["old_version"] == 1
    assert [(c["start_m"], c["end_m"]) for c in body["candidates"]] == [
        (500.0, 1000.0),
        (1000.0, 1500.0),
        (1500.0, 2000.0),
    ]
    # 分段端点参与换算：m=1000 同时是第 1 段止点、第 2 段起点。
    middle = body["candidates"][1]
    assert middle["start_segments"] == [{"seq": 1, "m": 1000.0, "station": 1000.0}]
    assert middle["end_segments"] == [{"seq": 2, "m": 1500.0, "station": 500.0}]


def test_locate_pending_survey_when_no_candidate(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    body = locate(client, measured_length=1234).json()
    assert body["status"] == "pending_survey"
    assert body["candidates"] == []


def test_locate_single_candidate_is_ready(client):
    make_road(client)
    make_version(client, "G101", [(0, 1000, 0), (1000, 2000, 500)], activate=True)
    # 桩号 200 只落在第 1 段（m=200），桩号 900 只落在第 1 段（m=900）。
    body = locate(client, start_station=900, end_station=200, measured_length=700).json()
    assert body["status"] == "ready"
    assert [(c["start_m"], c["end_m"]) for c in body["candidates"]] == [(200.0, 900.0)]


def test_confirm_requires_candidate_from_list(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client)
    response = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 100, "end_m": 600})
    assert response.status_code == 422

    response = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["confirmed"]["start_m"] == 1000
    assert body["confirmed"]["end_m"] == 1500
    assert body["confirmed"]["confirmed_version"] == 1
    assert body["supply"]["version_no"] == 1
    assert [(p["seq"], p["start_m"], p["end_m"]) for p in body["supply"]["pieces"]] == [
        (2, 1000.0, 1500.0)
    ]


def test_confirm_pending_survey_rejected(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client, measured_length=1234)
    response = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 0, "end_m": 500})
    assert response.status_code == 409


def test_confirmed_span_is_immutable(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client)
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})

    again = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 500, "end_m": 1000})
    assert again.status_code == 409
    relocalised = locate(client)
    assert relocalised.status_code == 409

    body = client.get("/roads/G101/damages/D-001").json()
    assert body["confirmed"]["start_m"] == 1000
    assert body["confirmed"]["end_m"] == 1500


def test_supply_follows_current_version(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client)
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})

    # 新表：实距轴不变，重划分段、重编桩号。
    new_segments = [
        (0, 800, 10000),
        (800, 1500, 10200),
        (1500, 2200, 10700),
    ]
    make_version(client, "G101", new_segments, label="换桩号", activate=True)

    body = client.get("/roads/G101/damages/D-001/supply").json()
    assert body["supply"]["version_no"] == 2
    assert body["supply"]["pieces"] == [
        {
            "seq": 2,
            "start_m": 1000.0,
            "end_m": 1500.0,
            "start_station": 10200 + 200,
            "end_station": 10200 + 700,
        }
    ]
    # 确认时固定的实距区段与版号不随换版改变。
    assert body["confirmed"]["start_m"] == 1000
    assert body["confirmed"]["end_m"] == 1500
    assert body["confirmed"]["confirmed_version"] == 1


def test_supply_splits_across_segments(client):
    make_road(client)
    make_version(client, "G101", [(0, 3000, 0)], activate=True)
    locate(client, start_station=1000, end_station=2000, measured_length=1000)
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 2000})

    make_version(client, "G101", [(0, 1500, 0), (1500, 3000, 1500)], activate=True)
    body = client.get("/roads/G101/damages/D-001/supply").json()
    pieces = body["supply"]["pieces"]
    assert [(p["seq"], p["start_station"], p["end_station"]) for p in pieces] == [
        (1, 1000.0, 1500.0),
        (2, 1500.0, 2000.0),
    ]
    assert body["supply"]["version_no"] == 2


def test_activation_rejected_on_gap(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client)
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})

    make_version(client, "G101", [(0, 900, 0), (1600, 2600, 0), (2600, 3000, 0)])
    response = client.post("/roads/G101/versions/2/activate")
    assert response.status_code == 409, response.text
    detail = response.json()
    assert detail["code"] == "coverage_gap"
    assert detail["gaps"] == [
        {"damage_no": "D-001", "missing": [[1000.0, 1500.0]]}
    ]

    # 现行表未被换掉，草稿仍可修改（重新登记新版本）。
    assert client.get("/roads/G101/current").json()["version_no"] == 1
    assert client.get("/roads/G101/versions/2").json()["status"] == "draft"


def test_direct_activate_rejected_on_gap_rolls_back(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    locate(client)
    client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})

    response = client.post(
        "/roads/G101/versions",
        json={
            "segments": [{"start_m": 0, "end_m": 1200, "start_station": 0}],
            "activate": True,
        },
    )
    assert response.status_code == 409
    assert response.json()["gaps"] == [
        {"damage_no": "D-001", "missing": [[1200.0, 1500.0]]}
    ]
    # 事务回滚：没有留下未生效的版本 2。
    assert [v["version_no"] for v in client.get("/roads/G101/versions").json()] == [1]


def test_gap_report_lists_every_confirmed_damage(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    for damage_no, span in (("D-001", (100, 600)), ("D-002", (200, 700))):
        locate(client, damage_no=damage_no, start_station=span[0], end_station=span[1], measured_length=500)
        assert (
            client.post(
                f"/roads/G101/damages/{damage_no}/confirm",
                json={"start_m": span[0], "end_m": span[1]},
            ).status_code
            == 200
        )

    make_version(client, "G101", [(300, 1100, 0)])
    response = client.post("/roads/G101/versions/2/activate")
    assert response.status_code == 409
    assert response.json()["gaps"] == [
        {"damage_no": "D-001", "missing": [[100.0, 300.0]]},
        {"damage_no": "D-002", "missing": [[200.0, 300.0]]},
    ]

def test_confirm_rejected_when_active_table_lacks_span(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    # 用旧表（版本 1）定位，得到 (1500, 2000) 等候选。
    body = locate(client, start_station=1000, end_station=500, measured_length=500).json()
    assert body["status"] == "needs_selection"

    # 换到只覆盖 0–1600 的现行表。
    make_version(client, "G101", [(0, 1600, 0)], activate=True)

    response = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1500, "end_m": 2000})
    assert response.status_code == 409
    detail = response.json()
    assert detail["code"] == "coverage_gap"
    assert detail["gaps"] == [{"damage_no": "D-001", "missing": [[1600.0, 2000.0]]}]

    # 未落库为已确认。
    assert client.get("/roads/G101/damages/D-001").json()["status"] == "needs_selection"

    # 覆盖范围内的候选仍可确认。
    ok = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 500, "end_m": 1000})
    assert ok.status_code == 200, ok.text
    assert ok.json()["supply"]["version_no"] == 2


def test_confirmation_accepted_while_new_table_is_draft(client):
    """准备新表（草稿）期间，确认照常受理并使用现行表。"""
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    make_version(client, "G101", [(0, 2000, 5000)], activate=False)

    body = locate(client).json()
    assert body["status"] == "needs_selection"
    response = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})
    assert response.status_code == 200, response.text
    assert response.json()["confirmed"]["confirmed_version"] == 1
    assert response.json()["supply"]["version_no"] == 1

    # 草稿版完整覆盖，可以随后生效；供数切到新表。
    activated = client.post("/roads/G101/versions/2/activate")
    assert activated.status_code == 200
    supply = client.get("/roads/G101/damages/D-001/supply").json()["supply"]
    assert supply["version_no"] == 2
    assert supply["pieces"] == [
        {
            "seq": 1,
            "start_m": 1000.0,
            "end_m": 1500.0,
            "start_station": 6000.0,
            "end_station": 6500.0,
        }
    ]


def test_activate_unknown_or_already_active(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    assert client.post("/roads/G101/versions/9/activate").status_code == 404
    assert client.post("/roads/G101/versions/1/activate").status_code == 409


def test_second_confirmation_still_accepted_after_new_table(client):
    """换表后照常受理新的确认，供数按新表现行版返回。"""
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    make_version(client, "G101", [(0, 2500, 0)], activate=True)

    # 用旧版（两段重号）定位 D-002，得到三个候选，任选其一确认。
    response = locate(
        client,
        damage_no="D-002",
        start_station=1000,
        end_station=500,
        measured_length=500,
    )
    assert response.status_code == 201
    candidates = response.json()["candidates"]
    assert [(c["start_m"], c["end_m"]) for c in candidates] == [
        (500.0, 1000.0),
        (1000.0, 1500.0),
        (1500.0, 2000.0),
    ]
    chosen = candidates[0]
    confirmed = client.post(
        "/roads/G101/damages/D-002/confirm",
        json={"start_m": chosen["start_m"], "end_m": chosen["end_m"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["supply"]["version_no"] == 2
    # 新表是一整段，供数只需一段。
    assert confirmed.json()["supply"]["pieces"] == [
        {
            "seq": 1,
            "start_m": 500.0,
            "end_m": 1000.0,
            "start_station": 500.0,
            "end_station": 1000.0,
        }
    ]


def test_damage_lookup_unknown(client):
    make_road(client)
    assert client.get("/roads/G101/damages/D-404").status_code == 404


def test_locate_unknown_version(client):
    make_road(client)
    assert locate(client).status_code == 404


def test_relocation_before_confirm_updates_record(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    first = locate(client).json()
    assert first["status"] == "needs_selection"
    second = locate(client, measured_length=1234).json()
    assert second["status"] == "pending_survey"
    assert second["candidates"] == []


def test_confirm_rejects_unknown_damage(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    response = client.post("/roads/NOPE/damages/D-001/confirm", json={"start_m": 0, "end_m": 500})
    assert response.status_code == 404


def relocate(client, damage_no="D-001"):
    """重新定位：输入与候选都被替换（候选变为 (200,900) 与 (1200,1900)）。"""
    return locate(client, damage_no=damage_no, start_station=900, end_station=200, measured_length=700)


def test_confirm_only_accepts_current_candidates_after_relocation(client):
    """重新定位替换候选后，旧候选不能再被确认，最新定位状态原样保留。"""
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    first = locate(client).json()
    assert first["status"] == "needs_selection"
    assert first["locate_seq"] == 1

    second = relocate(client).json()
    assert second["status"] == "needs_selection"
    assert second["locate_seq"] == 2
    assert [(c["start_m"], c["end_m"]) for c in second["candidates"]] == [
        (200.0, 900.0),
        (1200.0, 1900.0),
    ]

    # 旧定位的候选 (1000,1500) 已不在当前候选列表中，确认被拒绝。
    stale = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 1000, "end_m": 1500})
    assert stale.status_code == 422

    # 最新定位状态未被确认请求破坏。
    body = client.get("/roads/G101/damages/D-001").json()
    assert body["status"] == "needs_selection"
    assert body["locate_seq"] == 2
    assert body["confirmed"] is None
    assert [(c["start_m"], c["end_m"]) for c in body["candidates"]] == [
        (200.0, 900.0),
        (1200.0, 1900.0),
    ]

    # 当前候选仍可正常确认。
    ok = client.post("/roads/G101/damages/D-001/confirm", json={"start_m": 200, "end_m": 900})
    assert ok.status_code == 200, ok.text
    assert ok.json()["confirmed"]["start_m"] == 200


def test_confirm_with_stale_locate_seq_returns_409(client):
    """携带过期定位序号的确认返回 409 stale_location，并保留最新定位状态。"""
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    assert locate(client).json()["locate_seq"] == 1
    assert relocate(client).json()["locate_seq"] == 2

    # 即使区段本身在新候选中，基于旧定位序号的确认也被拒绝。
    stale = client.post(
        "/roads/G101/damages/D-001/confirm",
        json={"start_m": 200, "end_m": 900, "locate_seq": 1},
    )
    assert stale.status_code == 409
    detail = stale.json()
    assert detail["code"] == "stale_location"
    assert detail["locate_seq"] == 2

    body = client.get("/roads/G101/damages/D-001").json()
    assert body["status"] == "needs_selection"
    assert body["locate_seq"] == 2

    ok = client.post(
        "/roads/G101/damages/D-001/confirm",
        json={"start_m": 200, "end_m": 900, "locate_seq": 2},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed"


def test_locate_seq_increments_on_each_relocation(client):
    make_road(client)
    make_version(client, "G101", OLD_SEGMENTS, activate=True)
    assert locate(client).json()["locate_seq"] == 1
    # 无解重定位（pending_survey）同样推进序号。
    assert locate(client, measured_length=1234).json()["locate_seq"] == 2
    assert locate(client).json()["locate_seq"] == 3
    assert client.get("/roads/G101/damages/D-001").json()["locate_seq"] == 3
