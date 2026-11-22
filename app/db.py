"""SQLite 持久层。

表结构
------
``chains``
    公路（一条公路只有一条实测路距轴）。
``chain_versions``
    桩号表版本，状态为 ``draft`` / ``active`` / ``superseded``。
``chain_segments``
    某版桩号表的分段（起止实距米数 + 段首桩号），登记后不可改。
``damages``
    病害定位与确认记录。定位结果、确认后的固定实距区段都落库。

版号按公路单调递增；内容与版号一经登记即由数据库触发器锁定。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

DEFAULT_DB_PATH = os.environ.get(
    "CHAIN_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chains.db"),
)

_lock = threading.RLock()
_db_path: str = DEFAULT_DB_PATH


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


def get_db_path() -> str:
    return _db_path


class Store:
    """轻量 SQLite 封装；写入用 ``tx()`` 串行化。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _db_path
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with _lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chains (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chain_versions (
                    chain_id         TEXT NOT NULL REFERENCES chains(id),
                    version_no       INTEGER NOT NULL CHECK (version_no > 0),
                    status           TEXT NOT NULL CHECK (status IN ('draft', 'active', 'superseded')),
                    label            TEXT,
                    created_at       TEXT NOT NULL,
                    activated_at     TEXT,
                    superseded_at    TEXT,
                    PRIMARY KEY (chain_id, version_no)
                );

                -- 同一公路同时最多只有一个现行表
                CREATE UNIQUE INDEX IF NOT EXISTS ux_chain_active
                    ON chain_versions(chain_id) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS chain_segments (
                    chain_id         TEXT NOT NULL,
                    version_no       INTEGER NOT NULL,
                    seq              INTEGER NOT NULL CHECK (seq > 0),
                    start_m          REAL NOT NULL,
                    end_m            REAL NOT NULL,
                    start_station    REAL NOT NULL,
                    PRIMARY KEY (chain_id, version_no, seq),
                    CHECK (end_m > start_m),
                    FOREIGN KEY (chain_id, version_no)
                        REFERENCES chain_versions(chain_id, version_no)
                );

                CREATE TABLE IF NOT EXISTS damages (
                    chain_id              TEXT NOT NULL REFERENCES chains(id),
                    damage_no             TEXT NOT NULL,
                    status                TEXT NOT NULL CHECK (
                                              status IN ('pending_survey', 'needs_selection', 'ready', 'confirmed')),
                    old_version           INTEGER NOT NULL,
                    start_station         REAL NOT NULL,
                    end_station           REAL NOT NULL,
                    measured_length       REAL NOT NULL,
                    tolerance_m           REAL NOT NULL,
                    candidates_json       TEXT NOT NULL,
                    confirmed_version     INTEGER,
                    confirmed_start_m     REAL,
                    confirmed_end_m       REAL,
                    located_at            TEXT NOT NULL,
                    confirmed_at          TEXT,
                    PRIMARY KEY (chain_id, damage_no),
                    CHECK (
                        status <> 'confirmed' OR (
                            confirmed_version IS NOT NULL AND
                            confirmed_start_m IS NOT NULL AND
                            confirmed_end_m IS NOT NULL AND
                            confirmed_at IS NOT NULL
                        )
                    )
                );

                -- 版号与内容一经登记即固定：禁止改分段、禁止改版本标识
                CREATE TRIGGER IF NOT EXISTS trg_segments_no_update
                BEFORE UPDATE ON chain_segments
                BEGIN
                    SELECT RAISE(ABORT, '桩号表分段登记后不可修改');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_segments_no_delete
                BEFORE DELETE ON chain_segments
                BEGIN
                    SELECT RAISE(ABORT, '桩号表分段登记后不可删除');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_versions_content_fixed
                BEFORE UPDATE OF chain_id, version_no, label, created_at ON chain_versions
                BEGIN
                    SELECT RAISE(ABORT, '桩号表版号与内容登记后不可修改');
                END;

                -- 已确认的实距区段不可再改，也不可把记录改回未确认
                CREATE TRIGGER IF NOT EXISTS trg_damage_confirmed_fixed
                BEFORE UPDATE ON damages
                WHEN OLD.status = 'confirmed' AND (
                    NEW.status <> 'confirmed' OR
                    NEW.confirmed_version IS NOT OLD.confirmed_version OR
                    NEW.confirmed_start_m IS NOT OLD.confirmed_start_m OR
                    NEW.confirmed_end_m IS NOT OLD.confirmed_end_m OR
                    NEW.confirmed_at IS NOT OLD.confirmed_at
                )
                BEGIN
                    SELECT RAISE(ABORT, '已确认的实距区段不可修改');
                END;
                """
            )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with _lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with _lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None
