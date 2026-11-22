"""领域逻辑单元测试。

固定夹具（一条实距轴 0–3000m）：
* 段 A：0–1000m，段首桩号 0     → 桩号 0–1000
* 段 B：1000–2000m，段首桩号 0  → 桩号 0–1000（与 A 完全重号）
* 段 C：2000–3000m，段首桩号 5000 → 桩号 5000–6000（跳号）
"""

from __future__ import annotations

import pytest

from app.domain import (
    Segment,
    SegmentValidationError,
    build_segments,
    coverage_gaps,
    find_candidates,
    span_pieces,
    station_positions,
)


def segs():
    return build_segments([(0, 1000, 0), (1000, 2000, 0), (2000, 3000, 5000)])


def test_build_segments_sorts_and_rejects_overlap():
    built = build_segments([(2000, 3000, 5000), (0, 1000, 0)])
    assert [s.seq for s in built] == [1, 2]
    assert [s.start_m for s in built] == [0, 2000]

    with pytest.raises(SegmentValidationError):
        build_segments([(0, 1000, 0), (999, 2000, 0)])

    with pytest.raises(SegmentValidationError):
        build_segments([(0, 0, 0)])


def test_segments_may_share_only_endpoints():
    built = build_segments([(0, 1000, 0), (1000, 2000, 5000)])
    assert built[0].end_m == built[1].start_m
    # 实距重叠被拒；留空（非端点相接）则允许，覆盖由确认/换版时校验。
    with pytest.raises(SegmentValidationError):
        build_segments([(0, 1000, 0), (999.5, 2000, 5000)])
    assert len(build_segments([(0, 1000, 0), (1000.5, 2000, 5000)])) == 2


def test_station_endpoint_participates_in_multiple_segments():
    positions = station_positions(segs(), 1000)
    assert [p.m for p in positions] == [1000.0, 2000.0]
    assert positions[0].segment_seqs == [1]
    assert positions[1].segment_seqs == [2]


def test_station_outside_table_has_no_position():
    assert station_positions(segs(), -1) == []
    assert station_positions(segs(), 4000) == []


def test_duplicate_station_yields_distinct_candidates():
    # 桩号 200 在 m=200 与 m=1200 各有一个落点。
    candidates = find_candidates(segs(), 200, 200, 0.0)
    assert [(c.start_m, c.end_m) for c in candidates] == [(200.0, 200.0), (1200.0, 1200.0)]


def test_find_candidates_length_must_match():
    # 800 的落点 {800, 1800}，300 的落点 {300, 1300}
    candidates = find_candidates(segs(), 800, 300, 500)
    assert [(c.start_m, c.end_m) for c in candidates] == [
        (300.0, 800.0),
        (800.0, 1300.0),
        (1300.0, 1800.0),
    ]


def test_find_candidates_none_when_length_disagrees():
    assert find_candidates(segs(), 800, 300, 123.0) == []


def test_tolerance_absorbs_survey_rounding():
    candidates = find_candidates(segs(), 800, 300, 500.4, tolerance_m=0.5)
    assert len(candidates) == 3
    assert find_candidates(segs(), 800, 300, 500.6, tolerance_m=0.5) == []


def test_candidate_can_span_segment_junction():
    # 1000 落在 A 的端点 m=1000；400 落在 B 内 m=1400；长度 400 只此一段。
    candidates = find_candidates(segs(), 1000, 400, 400)
    assert len(candidates) == 1
    assert (candidates[0].start_m, candidates[0].end_m) == (1000.0, 1400.0)
    assert candidates[0].start_segment_seqs == [1]
    assert candidates[0].end_segment_seqs == [2]


def test_reverse_direction_reported():
    candidates = find_candidates(segs(), 5500, 5000, 500)
    assert len(candidates) == 1
    assert candidates[0].direction == "reverse"
    assert (candidates[0].start_m, candidates[0].end_m) == (2000.0, 2500.0)
    assert candidates[0].start_input == "end_station"
    assert candidates[0].end_input == "start_station"


def test_coverage_gaps():
    assert coverage_gaps(segs(), 500, 2500) == []
    assert coverage_gaps(segs(), -100, 3500) == [(-100.0, 0.0), (3000.0, 3500.0)]
    assert coverage_gaps(segs(), 1000, 2000) == []


def test_coverage_gaps_detects_hole():
    holed = build_segments([(0, 900, 0), (1100, 3000, 1100)])
    assert coverage_gaps(holed, 800, 1200) == [(900.0, 1100.0)]


def test_span_pieces_splits_on_current_table():
    pieces = span_pieces(segs(), 800, 2500)
    assert [(p["seq"], p["start_m"], p["end_m"]) for p in pieces] == [
        (1, 800.0, 1000.0),
        (2, 1000.0, 2000.0),
        (3, 2000.0, 2500.0),
    ]
    assert pieces[0]["start_station"] == pytest.approx(800.0)
    assert pieces[2]["end_station"] == pytest.approx(5500.0)


def test_span_pieces_single_segment():
    pieces = span_pieces(segs(), 1200, 1300)
    assert pieces == [
        {
            "seq": 2,
            "start_m": 1200,
            "end_m": 1300,
            "start_station": pytest.approx(200.0),
            "end_station": pytest.approx(300.0),
        }
    ]


def test_segment_station_arithmetic():
    seg = Segment(seq=1, start_m=100, end_m=200, start_station=5000)
    assert seg.end_station == pytest.approx(5100)
    assert seg.station_at(150) == pytest.approx(5050)
