"""SQLite 持久化层。

设计要点：

* 每条业务命令都在 ``BEGIN IMMEDIATE`` 事务内执行，SQLite 的写锁
  保证跨进程/跨线程并发下容量扣减不会超售；
* 容量余量不做冗余字段，统一由事件表实时汇总
  （approved 为 +seats，rejected/revoked 为 -seats），消除
  “表格版本互相覆盖”式的计数漂移；
* ``commands`` 表保存每个幂等键的原始命令与结果，既是去重依据，
  也让“审批中断后续跑”“服务重启后续办”成为可能——重放同一
  request_id 即可得到原结果；
* 申请单版本号单调递增，旧版本审批对新版本无效，避免陈旧批复覆盖现状。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    route_code   TEXT PRIMARY KEY,
    route_name   TEXT NOT NULL,
    total_seats  INTEGER NOT NULL CHECK (total_seats >= 0)
);

CREATE TABLE IF NOT EXISTS windows (
    route_code   TEXT NOT NULL,
    op_date      TEXT NOT NULL,
    batch_no     INTEGER NOT NULL,
    opens_at     TEXT NOT NULL,
    closes_at    TEXT NOT NULL,
    PRIMARY KEY (route_code, op_date, batch_no)
);

CREATE TABLE IF NOT EXISTS reviewers (
    route_code TEXT NOT NULL,
    username   TEXT NOT NULL,
    PRIMARY KEY (route_code, username)
);

CREATE TABLE IF NOT EXISTS requests (
    request_key   TEXT PRIMARY KEY,
    route_code    TEXT NOT NULL,
    op_date       TEXT NOT NULL,
    batch_no      INTEGER NOT NULL,
    city          TEXT NOT NULL,
    seats         INTEGER NOT NULL CHECK (seats > 0),
    status        TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    submitted_by  TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    review_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_lookup
    ON requests (route_code, op_date, batch_no, status);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    seats       INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_request ON events (request_key, id);

CREATE TABLE IF NOT EXISTS commands (
    request_id  TEXT PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Storage:
    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False：服务内部以事务串行化所有写入。
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """只读访问入口：查询不持写锁，且始终读到已提交的最新决定。"""
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即取写锁的事务，杜绝两个写事务同时读到旧余量。"""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---- 幂等命令 -------------------------------------------------------

    def get_command_result(self, conn: sqlite3.Connection, request_id: str) -> Optional[dict[str, Any]]:
        row = self.get_command(conn, request_id)
        return json.loads(row["result_json"]) if row else None

    def get_command(self, conn: sqlite3.Connection, request_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM commands WHERE request_id = ?", (request_id,)
        ).fetchone()

    def save_command_result(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        actor: str,
        action: str,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO commands (request_id, actor, action, payload, result_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, actor, action, json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)),
        )

    # ---- 基础数据 -------------------------------------------------------

    def upsert_route(self, conn: sqlite3.Connection, route_code: str,
                     route_name: str, total_seats: int) -> None:
        conn.execute(
            "INSERT INTO routes (route_code, route_name, total_seats) VALUES (?, ?, ?) "
            "ON CONFLICT(route_code) DO UPDATE SET "
            "route_name = excluded.route_name, total_seats = excluded.total_seats",
            (route_code, route_name, total_seats),
        )

    def get_route(self, conn: sqlite3.Connection, route_code: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM routes WHERE route_code = ?", (route_code,)).fetchone()

    def add_window(self, conn: sqlite3.Connection, route_code: str, op_date: str,
                   batch_no: int, opens_at: datetime, closes_at: datetime) -> None:
        conn.execute(
            "INSERT INTO windows (route_code, op_date, batch_no, opens_at, closes_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (route_code, op_date, batch_no, iso(opens_at), iso(closes_at)),
        )

    def get_window(self, conn: sqlite3.Connection, route_code: str,
                   op_date: str, batch_no: int) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM windows WHERE route_code = ? AND op_date = ? AND batch_no = ?",
            (route_code, op_date, batch_no),
        ).fetchone()

    def list_windows(self, conn: sqlite3.Connection, route_code: str,
                     op_date: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM windows WHERE route_code = ? AND op_date = ? ORDER BY batch_no",
            (route_code, op_date),
        ).fetchall()

    def grant_reviewer(self, conn: sqlite3.Connection, route_code: str, username: str) -> None:
        conn.execute(
            "INSERT INTO reviewers (route_code, username) VALUES (?, ?) "
            "ON CONFLICT(route_code, username) DO NOTHING",
            (route_code, username),
        )

    def is_reviewer(self, conn: sqlite3.Connection, route_code: str, username: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM reviewers WHERE route_code = ? AND username = ?",
            (route_code, username),
        ).fetchone() is not None

    # ---- 申请单 ---------------------------------------------------------

    def insert_request(self, conn: sqlite3.Connection, key: str, route_code: str,
                       op_date: str, batch_no: int, city: str, seats: int,
                       actor: str, submitted_at: datetime) -> None:
        conn.execute(
            "INSERT INTO requests (request_key, route_code, op_date, batch_no, city, "
            "seats, status, version, submitted_by, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)",
            (key, route_code, op_date, batch_no, city, seats, actor, iso(submitted_at)),
        )

    def get_request(self, conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM requests WHERE request_key = ?", (key,)).fetchone()

    def review_request(self, conn: sqlite3.Connection, key: str, status: str,
                       reviewer: str, reviewed_at: datetime, reason: str) -> None:
        conn.execute(
            "UPDATE requests SET status = ?, reviewed_by = ?, reviewed_at = ?, "
            "review_reason = ?, version = version + 1 WHERE request_key = ?",
            (status, reviewer, iso(reviewed_at), reason, key),
        )

    def update_pending_seats(self, conn: sqlite3.Connection, key: str, seats: int,
                             actor: str, occurred_at: datetime, reason: str) -> None:
        """待复核期间的需求修订：改座位数并推进版本，尚未占用任何配额。"""
        conn.execute(
            "UPDATE requests SET seats = ?, submitted_by = ?, submitted_at = ?, "
            "version = version + 1 WHERE request_key = ? AND status = 'pending'",
            (seats, actor, iso(occurred_at), key),
        )

    def reopen_request(self, conn: sqlite3.Connection, key: str, seats: int,
                       actor: str, occurred_at: datetime) -> None:
        """驳回/撤销后重新提交：回到 pending，清空旧复核结论，版本递增。"""
        conn.execute(
            "UPDATE requests SET seats = ?, status = 'pending', submitted_by = ?, "
            "submitted_at = ?, reviewed_by = NULL, reviewed_at = NULL, "
            "review_reason = NULL, version = version + 1 WHERE request_key = ?",
            (seats, actor, iso(occurred_at), key),
        )

    def list_requests(self, conn: sqlite3.Connection, route_code: str,
                      op_date: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM requests WHERE route_code = ? AND op_date = ? "
            "ORDER BY submitted_at, request_key",
            (route_code, op_date),
        ).fetchall()

    # ---- 事件与容量 -----------------------------------------------------

    def add_event(self, conn: sqlite3.Connection, request_key: str, event_type: str,
                  seats: int, actor: str, occurred_at: datetime, reason: str = "") -> None:
        conn.execute(
            "INSERT INTO events (request_key, event_type, seats, actor, reason, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request_key, event_type, seats, actor, reason, iso(occurred_at)),
        )

    def events_for(self, conn: sqlite3.Connection, request_key: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM events WHERE request_key = ? ORDER BY id", (request_key,)
        ).fetchall()

    def used_seats(self, conn: sqlite3.Connection, route_code: str,
                   op_date: str, batch_no: int) -> int:
        """某批次窗口内当前已占用的配额。

        占用只由事件符号决定：批准 +seats，撤销 -seats；驳回的申请
        从未占用配额，其事件不计入。窗口之间互不相干——逾期批次的
        占用永远不会计入新窗口。
        """
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE e.event_type "
            "WHEN 'approved' THEN e.seats "
            "WHEN 'revoked' THEN -e.seats ELSE 0 END), 0) AS total "
            "FROM events e JOIN requests r ON r.request_key = e.request_key "
            "WHERE r.route_code = ? AND r.op_date = ? AND r.batch_no = ?",
            (route_code, op_date, batch_no),
        ).fetchone()
        return int(row["total"])
