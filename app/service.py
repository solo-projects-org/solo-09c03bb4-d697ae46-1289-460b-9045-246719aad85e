"""业务服务：登记桩号表、定位病害、确认区段、换版与供数。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .domain import (
    DEFAULT_TOLERANCE_M,
    POS_EPS,
    Candidate,
    Segment,
    SegmentValidationError,
    build_segments,
    coverage_gaps,
    find_candidates,
    span_pieces,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gap_error(message: str, gaps: list[dict[str, Any]]) -> ServiceError:
    return ServiceError(
        409,
        {
            "code": "coverage_gap",
            "message": message,
            "gaps": gaps,
        },
    )


def _collect_gaps(confirmed: Sequence[Any], segments: Sequence[Segment]) -> list[dict[str, Any]]:
    """返回每个已确认病害相对 ``segments`` 的缺失实距区间。"""

    gaps: list[dict[str, Any]] = []
    for damage in confirmed:
        missing = coverage_gaps(
            segments,
            damage["confirmed_start_m"],
            damage["confirmed_end_m"],
        )
        if missing:
            gaps.append(
                {
                    "damage_no": damage["damage_no"],
                    "missing": [[round(a, 6), round(b, 6)] for a, b in missing],
                }
            )
    return gaps


class ChainService:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ---------- 公路 ----------
    def create_road(self, road_id: str, name: str) -> dict[str, Any]:
        try:
            with self.store.tx() as conn:
                conn.execute(
                    "INSERT INTO chains (id, name, created_at) VALUES (?, ?, ?)",
                    (road_id, name, _now()),
                )
        except sqlite3.IntegrityError:
            raise ServiceError(409, f"公路 {road_id} 已存在")
        return self.get_road(road_id)

    def get_road(self, road_id: str) -> dict[str, Any]:
        row = self.store.query_one("SELECT id, name, created_at FROM chains WHERE id = ?", (road_id,))
        if row is None:
            raise ServiceError(404, f"公路 {road_id} 不存在")
        return dict(row)

    def list_roads(self) -> list[dict[str, Any]]:
        rows = self.store.query("SELECT id, name, created_at FROM chains ORDER BY created_at, id")
        return [dict(row) for row in rows]

    # ---------- 桩号表版本 ----------
    def _segments_for(self, road_id: str, version_no: int) -> list[Segment]:
        rows = self.store.query(
            "SELECT seq, start_m, end_m, start_station FROM chain_segments "
            "WHERE chain_id = ? AND version_no = ? ORDER BY seq",
            (road_id, version_no),
        )
        if not rows:
            self._require_version(road_id, version_no)
            return []
        return [
            Segment(seq=row["seq"], start_m=row["start_m"], end_m=row["end_m"], start_station=row["start_station"])
            for row in rows
        ]

    def _require_version(self, road_id: str, version_no: int) -> sqlite3.Row:
        row = self.store.query_one(
            "SELECT * FROM chain_versions WHERE chain_id = ? AND version_no = ?",
            (road_id, version_no),
        )
        if row is None:
            raise ServiceError(404, f"公路 {road_id} 不存在桩号表版本 {version_no}")
        return row

    def _active_version(self, road_id: str) -> sqlite3.Row:
        row = self.store.query_one(
            "SELECT * FROM chain_versions WHERE chain_id = ? AND status = 'active'",
            (road_id,),
        )
        if row is None:
            raise ServiceError(409, f"公路 {road_id} 当前没有生效的桩号表")
        return row

    def create_version(
        self,
        road_id: str,
        raw_segments: Sequence[tuple[float, float, float]],
        label: Optional[str] = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        self.get_road(road_id)
        try:
            segments = build_segments(raw_segments)
        except SegmentValidationError as exc:
            raise ServiceError(422, str(exc))
        if not segments:
            raise ServiceError(422, "桩号表至少需要一个分段")

        # 要求直接生效时，登记草稿并在同一事务内校验覆盖、激活；
        # 有缺口则整个事务回滚，不会留下半成品版本。
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS max_no FROM chain_versions WHERE chain_id = ?",
                (road_id,),
            ).fetchone()
            version_no = int(row["max_no"]) + 1
            conn.execute(
                "INSERT INTO chain_versions (chain_id, version_no, status, label, created_at) "
                "VALUES (?, ?, 'draft', ?, ?)",
                (road_id, version_no, label, _now()),
            )
            for seg in segments:
                conn.execute(
                    "INSERT INTO chain_segments (chain_id, version_no, seq, start_m, end_m, start_station) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (road_id, version_no, seg.seq, seg.start_m, seg.end_m, seg.start_station),
                )
            if activate:
                gaps = self._gaps_within(conn, road_id, segments)
                if gaps:
                    raise _gap_error(
                        "桩号表未完整覆盖已确认病害区段，拒绝登记生效",
                        gaps,
                    )
                self._activate_within(conn, road_id, version_no)

        return self.get_version(road_id, version_no)

    def get_version(self, road_id: str, version_no: int) -> dict[str, Any]:
        row = self._require_version(road_id, version_no)
        return self._version_payload(row)

    def list_versions(self, road_id: str) -> list[dict[str, Any]]:
        self.get_road(road_id)
        rows = self.store.query(
            "SELECT * FROM chain_versions WHERE chain_id = ? ORDER BY version_no",
            (road_id,),
        )
        return [self._version_payload(row) for row in rows]

    def current_version(self, road_id: str) -> dict[str, Any]:
        return self._version_payload(self._active_version(road_id))

    def _version_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        segments = self._segments_for(row["chain_id"], row["version_no"])
        return {
            "chain_id": row["chain_id"],
            "version_no": row["version_no"],
            "status": row["status"],
            "label": row["label"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "superseded_at": row["superseded_at"],
            "segments": [asdict(seg) for seg in segments],
        }

    @staticmethod
    def _gaps_within(
        conn: sqlite3.Connection, road_id: str, segments: Sequence[Segment]
    ) -> list[dict[str, Any]]:
        """在事务内计算已确认区段相对 ``segments`` 的覆盖缺口。"""

        confirmed = list(
            conn.execute(
                "SELECT damage_no, confirmed_start_m, confirmed_end_m FROM damages "
                "WHERE chain_id = ? AND status = 'confirmed' ORDER BY damage_no",
                (road_id,),
            )
        )
        return _collect_gaps(confirmed, segments)

    def activate_version(self, road_id: str, version_no: int) -> dict[str, Any]:
        """换版：校验后让指定版本生效，旧现行表转为 superseded。

        新表必须完整覆盖全部已确认区段；有缺口则整次操作拒绝，不落任何改动。
        """

        self.get_road(road_id)
        with self.store.tx() as conn:
            target = conn.execute(
                "SELECT * FROM chain_versions WHERE chain_id = ? AND version_no = ?",
                (road_id, version_no),
            ).fetchone()
            if target is None:
                raise ServiceError(404, f"公路 {road_id} 不存在桩号表版本 {version_no}")
            if target["status"] == "active":
                raise ServiceError(409, f"版本 {version_no} 已是现行表")
            if target["status"] == "superseded":
                raise ServiceError(409, f"版本 {version_no} 已被替换，不能再生效")

            new_segments = self._segments_for(road_id, version_no)
            if not new_segments:
                raise ServiceError(422, f"版本 {version_no} 没有分段，不能生效")

            gaps = self._gaps_within(conn, road_id, new_segments)
            if gaps:
                raise _gap_error(
                    f"版本 {version_no} 未完整覆盖已确认病害区段，拒绝换版",
                    gaps,
                )

            self._activate_within(conn, road_id, version_no)

        return self.get_version(road_id, version_no)

    @staticmethod
    def _activate_within(conn: sqlite3.Connection, road_id: str, version_no: int) -> None:
        """在已开启的事务中把 ``version_no`` 设为现行，旧现行表转为 superseded。"""

        old_active = conn.execute(
            "SELECT version_no FROM chain_versions WHERE chain_id = ? AND status = 'active'",
            (road_id,),
        ).fetchone()
        if old_active is not None:
            conn.execute(
                "UPDATE chain_versions SET status = 'superseded', superseded_at = ? "
                "WHERE chain_id = ? AND version_no = ?",
                (_now(), road_id, old_active["version_no"]),
            )
        conn.execute(
            "UPDATE chain_versions SET status = 'active', activated_at = ? "
            "WHERE chain_id = ? AND version_no = ?",
            (_now(), road_id, version_no),
        )

    # ---------- 定位 ----------
    def locate(
        self,
        road_id: str,
        damage_no: str,
        old_version: int,
        start_station: float,
        end_station: float,
        measured_length: float,
        tolerance_m: float = DEFAULT_TOLERANCE_M,
    ) -> dict[str, Any]:
        self.get_road(road_id)
        self._require_version(road_id, old_version)
        if measured_length <= 0:
            raise ServiceError(422, "实测长度必须大于 0")
        if tolerance_m < 0:
            raise ServiceError(422, "容差不能为负数")

        segments = self._segments_for(road_id, old_version)
        candidates = find_candidates(segments, start_station, end_station, measured_length, tolerance_m)
        if not candidates:
            status = "pending_survey"
        elif len(candidates) == 1:
            status = "ready"
        else:
            status = "needs_selection"

        payload = [self._candidate_payload(cand, segments) for cand in candidates]
        now = _now()

        with self.store.tx() as conn:
            existing = conn.execute(
                "SELECT status FROM damages WHERE chain_id = ? AND damage_no = ?",
                (road_id, damage_no),
            ).fetchone()
            if existing is not None and existing["status"] == "confirmed":
                raise ServiceError(
                    409,
                    f"病害 {damage_no} 已确认并固定实距区段，不能再重新定位",
                )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO damages (
                        chain_id, damage_no, status, old_version, start_station, end_station,
                        measured_length, tolerance_m, candidates_json, located_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        road_id,
                        damage_no,
                        status,
                        old_version,
                        start_station,
                        end_station,
                        measured_length,
                        tolerance_m,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE damages SET
                        status = ?, old_version = ?, start_station = ?, end_station = ?,
                        measured_length = ?, tolerance_m = ?, candidates_json = ?, located_at = ?
                    WHERE chain_id = ? AND damage_no = ?
                    """,
                    (
                        status,
                        old_version,
                        start_station,
                        end_station,
                        measured_length,
                        tolerance_m,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                        road_id,
                        damage_no,
                    ),
                )

        return self.get_damage(road_id, damage_no, include_supply=False)

    def _candidate_payload(self, candidate: Candidate, segments: Sequence[Segment]) -> dict[str, Any]:
        by_seq = {seg.seq: seg for seg in segments}
        return {
            "start_m": round(candidate.start_m, 6),
            "end_m": round(candidate.end_m, 6),
            "length_m": round(candidate.length_m, 6),
            "direction": candidate.direction,
            "start_input": candidate.start_input,
            "end_input": candidate.end_input,
            "start_segments": [
                self._segment_ref(by_seq[seq], candidate.start_m)
                for seq in sorted(candidate.start_segment_seqs)
            ],
            "end_segments": [
                self._segment_ref(by_seq[seq], candidate.end_m)
                for seq in sorted(candidate.end_segment_seqs)
            ],
        }

    @staticmethod
    def _segment_ref(segment: Segment, m: float) -> dict[str, Any]:
        return {
            "seq": segment.seq,
            "m": round(m, 6),
            "station": round(segment.station_at(m), 6),
        }

    # ---------- 确认 ----------
    def confirm(
        self,
        road_id: str,
        damage_no: str,
        start_m: float,
        end_m: float,
    ) -> dict[str, Any]:
        self.get_road(road_id)
        row = self.store.query_one(
            "SELECT * FROM damages WHERE chain_id = ? AND damage_no = ?",
            (road_id, damage_no),
        )
        if row is None:
            raise ServiceError(404, f"病害 {damage_no} 尚未定位")
        if row["status"] == "confirmed":
            raise ServiceError(409, f"病害 {damage_no} 已确认，确认记录不可修改")
        if row["status"] == "pending_survey":
            raise ServiceError(409, f"病害 {damage_no} 无候选区段，待补测，不能确认")

        candidates = json.loads(row["candidates_json"])
        matched = None
        for cand in candidates:
            if abs(cand["start_m"] - min(start_m, end_m)) <= POS_EPS and abs(
                cand["end_m"] - max(start_m, end_m)
            ) <= POS_EPS:
                matched = cand
                break
        if matched is None:
            raise ServiceError(422, "所选实距区段不在本病害的候选列表中")

        span_start, span_end = matched["start_m"], matched["end_m"]

        with self.store.tx() as conn:
            current = conn.execute(
                "SELECT status FROM damages WHERE chain_id = ? AND damage_no = ?",
                (road_id, damage_no),
            ).fetchone()
            if current is None or current["status"] == "confirmed":
                raise ServiceError(409, "病害确认状态已变化，请重新查询")

            active = conn.execute(
                "SELECT version_no FROM chain_versions WHERE chain_id = ? AND status = 'active'",
                (road_id,),
            ).fetchone()
            if active is None:
                raise ServiceError(409, f"公路 {road_id} 当前没有生效的桩号表")
            active_segments = self._segments_for(road_id, active["version_no"])
            missing = coverage_gaps(active_segments, span_start, span_end)
            if missing:
                raise _gap_error(
                    f"现行表（版本 {active['version_no']}）未完整覆盖病害 {damage_no} 的确认区段，拒绝确认",
                    [
                        {
                            "damage_no": damage_no,
                            "missing": [[round(a, 6), round(b, 6)] for a, b in missing],
                        }
                    ],
                )

            conn.execute(
                """
                UPDATE damages SET
                    status = 'confirmed',
                    confirmed_version = ?,
                    confirmed_start_m = ?,
                    confirmed_end_m = ?,
                    confirmed_at = ?
                WHERE chain_id = ? AND damage_no = ?
                """,
                (
                    active["version_no"],
                    span_start,
                    span_end,
                    _now(),
                    road_id,
                    damage_no,
                ),
            )

        return self.get_damage(road_id, damage_no)

    # ---------- 供数 ----------
    def get_damage(self, road_id: str, damage_no: str, include_supply: bool = True) -> dict[str, Any]:
        self.get_road(road_id)
        row = self.store.query_one(
            "SELECT * FROM damages WHERE chain_id = ? AND damage_no = ?",
            (road_id, damage_no),
        )
        if row is None:
            raise ServiceError(404, f"病害 {damage_no} 尚未定位")

        payload: dict[str, Any] = {
            "chain_id": road_id,
            "damage_no": damage_no,
            "status": row["status"],
            "located_at": row["located_at"],
            "old_version": row["old_version"],
            "input": {
                "start_station": row["start_station"],
                "end_station": row["end_station"],
                "measured_length": row["measured_length"],
                "tolerance_m": row["tolerance_m"],
            },
            "candidates": json.loads(row["candidates_json"]),
        }
        if row["status"] == "confirmed":
            payload["confirmed"] = {
                "start_m": row["confirmed_start_m"],
                "end_m": row["confirmed_end_m"],
                "length_m": round(row["confirmed_end_m"] - row["confirmed_start_m"], 6),
                "confirmed_version": row["confirmed_version"],
                "confirmed_at": row["confirmed_at"],
            }
        else:
            payload["confirmed"] = None

        if include_supply and row["status"] == "confirmed":
            active = self._active_version(road_id)
            segments = self._segments_for(road_id, active["version_no"])
            pieces = span_pieces(segments, row["confirmed_start_m"], row["confirmed_end_m"])
            payload["supply"] = {
                "version_no": active["version_no"],
                "version_status": active["status"],
                "pieces": pieces,
            }
        else:
            payload["supply"] = None
        return payload

    def list_damages(self, road_id: str) -> list[dict[str, Any]]:
        self.get_road(road_id)
        rows = self.store.query(
            "SELECT damage_no FROM damages WHERE chain_id = ? ORDER BY damage_no",
            (road_id,),
        )
        return [self.get_damage(road_id, row["damage_no"], include_supply=False) for row in rows]
