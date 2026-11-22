"""纯领域逻辑：桩号表校验、桩号↔实距换算、候选区段枚举、覆盖检查。

约定
----
* 一条公路只有一条实测路距轴，单位为米。
* 桩号表由若干分段组成；每段给出 ``[start_m, end_m]`` 和段首桩号。
* 段内桩号随米数 1:1 递增，因此 ``end_station = start_station + (end_m - start_m)``。
* 同一版内各段在实距上不得重叠，只允许在端点相接。
* 段与段之间桩号可跳号、可重号，因此同一桩号可能落在多个实距位置。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Sequence

# 浮点比较容差
EPS = 1e-9
POS_EPS = 1e-6
STATION_EPS = 1e-6
# 默认长度吻合容差：0.1 米，吸收实测/记录的舍入误差；可按需收紧或放宽。
DEFAULT_TOLERANCE_M = 0.1
DEDUPE_DIGITS = 6


class SegmentValidationError(ValueError):
    """桩号表分段非法。"""


@dataclass(frozen=True)
class Segment:
    seq: int
    start_m: float
    end_m: float
    start_station: float

    @property
    def end_station(self) -> float:
        return self.start_station + (self.end_m - self.start_m)

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m

    def station_at(self, m: float) -> float:
        return self.start_station + (m - self.start_m)


@dataclass
class Position:
    """某个桩号在实距轴上的一个落点。"""

    m: float
    segment_seqs: list[int]


@dataclass
class Candidate:
    """一个长度吻合的实距区段。

    ``start_input`` / ``end_input`` 说明落在低实距端和高实距端的分别是请求里的
    哪个桩号（``start_station`` 或 ``end_station``），便于人工核对方向。
    """

    start_m: float
    end_m: float
    direction: str
    start_segment_seqs: list[int]
    end_segment_seqs: list[int]
    start_input: str = "start_station"
    end_input: str = "end_station"

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


def build_segments(raw: Iterable[tuple[float, float, float]]) -> list[Segment]:
    """校验并规范化分段。

    ``raw`` 中每项为 ``(start_m, end_m, start_station)``。返回按实距排序、
    ``seq`` 从 1 重新编号的分段列表。
    """

    segments: list[Segment] = []
    for index, (start_m, end_m, start_station) in enumerate(raw, start=1):
        if end_m - start_m <= EPS:
            raise SegmentValidationError(f"第 {index} 段 end_m 必须大于 start_m")
        segments.append(Segment(seq=0, start_m=float(start_m), end_m=float(end_m), start_station=float(start_station)))

    segments.sort(key=lambda seg: (seg.start_m, seg.end_m))
    for index in range(1, len(segments)):
        previous = segments[index - 1]
        current = segments[index]
        if current.start_m < previous.end_m - EPS:
            raise SegmentValidationError(
                f"第 {index + 1} 段（{current.start_m:g}m 起）与上一段（{previous.end_m:g}m 止）实距重叠；"
                "各段只允许在端点相接"
            )
    return [replace(seg, seq=index) for index, seg in enumerate(segments, start=1)]


def station_positions(segments: Sequence[Segment], station: float) -> list[Position]:
    """求桩号在实测路距轴上的全部落点（含分段端点）。

    段间跳号/重号会让同一桩号落在多个实距位置；相邻段共享实距端点时，
    该端点处可能出现两个不同的桩号，同样各自成为一个落点。
    """

    raw: list[tuple[float, int]] = []
    for seg in segments:
        if station < seg.start_station - STATION_EPS or station > seg.end_station + STATION_EPS:
            continue
        # 桩号→实距的反向换算
        m = seg.start_m + (station - seg.start_station)
        # 端点换算可能产生极小浮点误差，夹回段内。
        m = min(max(m, seg.start_m), seg.end_m)
        raw.append((m, seg.seq))

    raw.sort(key=lambda item: item[0])
    merged: list[Position] = []
    for m, seq in raw:
        if merged and abs(m - merged[-1].m) <= POS_EPS:
            if seq not in merged[-1].segment_seqs:
                merged[-1].segment_seqs.append(seq)
        else:
            merged.append(Position(m=m, segment_seqs=[seq]))
    return merged


def find_candidates(
    segments: Sequence[Segment],
    start_station: float,
    end_station: float,
    measured_length: float,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> list[Candidate]:
    """枚举与实测长度吻合的不同实距区段。

    两端桩号不分先后：对两个桩号的每个落点组合，若实距差的绝对值与
    实测长度在容差内吻合，即得到一个候选区段。相同区段会合并。
    """

    positions_a = station_positions(segments, start_station)
    positions_b = station_positions(segments, end_station)

    merged: dict[tuple[float, float], Candidate] = {}
    for pos_a in positions_a:
        for pos_b in positions_b:
            if abs(abs(pos_b.m - pos_a.m) - measured_length) > tolerance_m:
                continue
            if pos_a.m <= pos_b.m:
                lower, upper = pos_a, pos_b
                direction = "forward"
                start_input, end_input = "start_station", "end_station"
            else:
                lower, upper = pos_b, pos_a
                direction = "reverse"
                start_input, end_input = "end_station", "start_station"
            key = (round(lower.m, DEDUPE_DIGITS), round(upper.m, DEDUPE_DIGITS))
            if key not in merged:
                merged[key] = Candidate(
                    start_m=lower.m,
                    end_m=upper.m,
                    direction=direction,
                    start_segment_seqs=list(lower.segment_seqs),
                    end_segment_seqs=list(upper.segment_seqs),
                    start_input=start_input,
                    end_input=end_input,
                )
            else:
                candidate = merged[key]
                for seq in lower.segment_seqs:
                    if seq not in candidate.start_segment_seqs:
                        candidate.start_segment_seqs.append(seq)
                for seq in upper.segment_seqs:
                    if seq not in candidate.end_segment_seqs:
                        candidate.end_segment_seqs.append(seq)

    return sorted(merged.values(), key=lambda cand: (cand.start_m, cand.end_m))


def coverage_gaps(
    segments: Sequence[Segment],
    start_m: float,
    end_m: float,
    eps: float = POS_EPS,
) -> list[tuple[float, float]]:
    """求 ``[start_m, end_m]`` 中未被分段覆盖的实距区间。"""

    if end_m < start_m:
        start_m, end_m = end_m, start_m
    ordered = sorted(segments, key=lambda seg: (seg.start_m, seg.end_m))
    cursor = start_m
    gaps: list[tuple[float, float]] = []

    for seg in ordered:
        if seg.end_m <= cursor + eps:
            continue
        if seg.start_m >= end_m - eps:
            break
        if seg.start_m > cursor + eps:
            gaps.append((cursor, min(seg.start_m, end_m)))
        if seg.end_m > cursor:
            cursor = seg.end_m
        if cursor >= end_m - eps:
            break
    if cursor < end_m - eps:
        gaps.append((cursor, end_m))
    return gaps


def span_pieces(segments: Sequence[Segment], start_m: float, end_m: float) -> list[dict]:
    """沿现行表分段切分已确认区段，返回每段的桩号范围。"""

    if end_m < start_m:
        start_m, end_m = end_m, start_m
    ordered = sorted(segments, key=lambda seg: (seg.start_m, seg.end_m))
    pieces: list[dict] = []
    for seg in ordered:
        piece_start = max(start_m, seg.start_m)
        piece_end = min(end_m, seg.end_m)
        if piece_end - piece_start <= EPS:
            continue
        pieces.append(
            {
                "seq": seg.seq,
                "start_m": piece_start,
                "end_m": piece_end,
                "start_station": seg.station_at(piece_start),
                "end_station": seg.station_at(piece_end),
            }
        )
    return pieces
