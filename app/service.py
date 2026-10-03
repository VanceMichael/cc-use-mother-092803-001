"""跨区域运力调度：容量校验、时间窗口锁定与复核决策。

状态机：城市按运营日 submit 申请后进入 pending_review（此时已完成
容量校验并锁定窗口配额），由有权限的复核人 approve / reject 进入终态。
窗口关闭时仍未复核的批次自动置为 expired 并释放配额，不会挤占后续窗口。

所有状态持久化在 SQLite 中：同一 request_id 重试直接返回首次结果、
不重复占用配额；审批中断或服务重启后从原进度继续。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Iterable, Optional

from .contracts import Request, Result, utcnow, validate_request

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    window_id   TEXT PRIMARY KEY,
    route_id    TEXT NOT NULL,
    service_date TEXT NOT NULL,
    open_at     TEXT NOT NULL,
    close_at    TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    reserved    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS demands (
    demand_id   TEXT PRIMARY KEY,
    city        TEXT NOT NULL,
    route_id    TEXT NOT NULL,
    service_date TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    window_id   TEXT NOT NULL,
    state       TEXT NOT NULL,
    reason      TEXT,
    decided_by  TEXT,
    decided_at  TEXT,
    version     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    request_id  TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    result      TEXT NOT NULL
);
"""

PENDING = "pending_review"
TERMINAL_STATES = ("approved", "rejected", "expired")


def _fingerprint(request: Request) -> str:
    return json.dumps(
        {"actor": request.actor, "action": request.action, "payload": request.payload},
        sort_keys=True,
        ensure_ascii=False,
    )


def _result_json(result: Result) -> str:
    return json.dumps(
        {"accepted": result.accepted, "state": result.state,
         "message": result.message, "data": result.data},
        ensure_ascii=False,
    )


class RouteControlService:
    """运力决策服务。db_path 指向 SQLite 文件（":memory:" 表示纯内存）。"""

    def __init__(
        self,
        db_path: str = ":memory:",
        reviewers: Iterable[str] = (),
        schedulers: Iterable[str] = (),
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._reviewers = set(reviewers)
        self._schedulers = set(schedulers)
        self._now = now or utcnow
        self._lock = RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ 入口

    def handle(self, request: Request) -> Result:
        validate_request(request)
        with self._lock:
            receipt = self._conn.execute(
                "SELECT fingerprint, result FROM receipts WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if receipt is not None:
                if receipt["fingerprint"] != _fingerprint(request):
                    return Result(False, "conflict", "幂等键已被不同请求占用")
                return Result(**json.loads(receipt["result"]))

            if request.action == "query":
                return self._query(request)

            try:
                self._expire_overdue()
                if request.action == "open_window":
                    result = self._open_window(request)
                elif request.action == "submit":
                    result = self._submit(request)
                elif request.action in ("approve", "reject"):
                    result = self._review(request)
                else:
                    result = Result(False, "unknown_action", f"不支持的动作: {request.action}")
                self._conn.execute(
                    "INSERT INTO receipts (request_id, fingerprint, result) VALUES (?, ?, ?)",
                    (request.request_id, _fingerprint(request), _result_json(result)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return result

    # -------------------------------------------------------------- 窗口管理

    def _open_window(self, request: Request) -> Result:
        if request.actor not in self._schedulers:
            return Result(False, "forbidden", "无窗口管理权限")
        p = request.payload
        window_id = str(p.get("window_id", "")).strip()
        route_id = str(p.get("route_id", "")).strip()
        service_date = str(p.get("service_date", "")).strip()
        open_at = str(p.get("open_at", "")).strip()
        close_at = str(p.get("close_at", "")).strip()
        capacity = p.get("capacity")
        if not all([window_id, route_id, service_date, open_at, close_at]):
            return Result(False, "invalid", "窗口缺少线路、运营日或起止时间")
        if not isinstance(capacity, int) or capacity <= 0:
            return Result(False, "invalid", "窗口容量必须为正整数")
        if not close_at > open_at:
            return Result(False, "invalid", "窗口关闭时间必须晚于开放时间")
        try:
            self._conn.execute(
                "INSERT INTO windows (window_id, route_id, service_date, open_at, close_at, capacity)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (window_id, route_id, service_date, open_at, close_at, capacity),
            )
        except sqlite3.IntegrityError:
            return Result(False, "duplicate", "窗口已存在", {"window_id": window_id})
        return Result(True, "window_open", "窗口已开放", {"window_id": window_id, "capacity": capacity})

    # -------------------------------------------------------------- 提交申请

    def _submit(self, request: Request) -> Result:
        p = request.payload
        city = str(p.get("city", "")).strip()
        route_id = str(p.get("route_id", "")).strip()
        service_date = str(p.get("service_date", "")).strip()
        quantity = p.get("quantity")
        if not all([city, route_id, service_date]):
            return Result(False, "invalid", "缺少城市、线路或运营日")
        if not isinstance(quantity, int) or quantity <= 0:
            return Result(False, "invalid", "申请数量必须为正整数")

        now = self._now().isoformat()
        window = self._find_window(route_id, service_date, str(p.get("window_id", "")).strip(), now)
        if window is None:
            return Result(False, "no_window", "当前没有可用的时间窗口",
                          {"route_id": route_id, "service_date": service_date})

        remaining = window["capacity"] - window["reserved"]
        if quantity > remaining:
            return Result(False, "capacity_exceeded", "剩余容量不足",
                          {"window_id": window["window_id"], "remaining": remaining})

        self._conn.execute(
            "INSERT INTO demands (demand_id, city, route_id, service_date, quantity, window_id,"
            " state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request.request_id, city, route_id, service_date, quantity,
             window["window_id"], PENDING, now, now),
        )
        self._conn.execute(
            "UPDATE windows SET reserved = reserved + ? WHERE window_id = ?",
            (quantity, window["window_id"]),
        )
        return Result(True, PENDING, "容量校验通过，已锁定窗口配额，待复核", {
            "demand_id": request.request_id,
            "window_id": window["window_id"],
            "remaining": remaining - quantity,
        })

    def _find_window(self, route_id: str, service_date: str, window_id: str, now: str):
        if window_id:
            return self._conn.execute(
                "SELECT * FROM windows WHERE window_id = ? AND route_id = ? AND service_date = ?"
                " AND open_at <= ? AND close_at > ?",
                (window_id, route_id, service_date, now, now),
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM windows WHERE route_id = ? AND service_date = ?"
            " AND open_at <= ? AND close_at > ? ORDER BY open_at DESC LIMIT 1",
            (route_id, service_date, now, now),
        ).fetchone()

    # ------------------------------------------------------------------ 复核

    def _review(self, request: Request) -> Result:
        if request.actor not in self._reviewers:
            return Result(False, "forbidden", "无复核权限")
        demand_id = str(request.payload.get("demand_id", "")).strip()
        row = self._conn.execute(
            "SELECT * FROM demands WHERE demand_id = ?", (demand_id,),
        ).fetchone()
        if row is None:
            return Result(False, "not_found", "申请不存在", {"demand_id": demand_id})
        if row["state"] != PENDING:
            return Result(False, row["state"], "该申请已结案，当前决定仍然有效",
                          self._decision_data(row))

        now = self._now().isoformat()
        if request.action == "approve":
            state, reason = "approved", str(request.payload.get("reason") or "复核通过")
        else:
            state, reason = "rejected", str(request.payload.get("reason") or "复核未通过")
            self._conn.execute(
                "UPDATE windows SET reserved = reserved - ? WHERE window_id = ?",
                (row["quantity"], row["window_id"]),
            )
        self._conn.execute(
            "UPDATE demands SET state = ?, reason = ?, decided_by = ?, decided_at = ?,"
            " version = version + 1, updated_at = ? WHERE demand_id = ?",
            (state, reason, request.actor, now, now, demand_id),
        )
        message = "已批复" if state == "approved" else "已驳回，配额已释放"
        return Result(True, state, message, {"demand_id": demand_id, "decided_by": request.actor})

    # ------------------------------------------------------------------ 查询

    def _query(self, request: Request) -> Result:
        self._expire_overdue()
        self._conn.commit()
        demand_id = str(request.payload.get("demand_id", "")).strip()
        row = self._conn.execute(
            "SELECT * FROM demands WHERE demand_id = ?", (demand_id,),
        ).fetchone()
        if row is None:
            return Result(False, "not_found", "申请不存在", {"demand_id": demand_id})
        return Result(True, row["state"], "当前有效决定", self._decision_data(row))

    @staticmethod
    def _decision_data(row) -> dict[str, Any]:
        return {
            "demand_id": row["demand_id"],
            "city": row["city"],
            "route_id": row["route_id"],
            "service_date": row["service_date"],
            "quantity": row["quantity"],
            "window_id": row["window_id"],
            "state": row["state"],
            "reason": row["reason"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "version": row["version"],
        }

    # -------------------------------------------------------------- 逾期处理

    def _expire_overdue(self) -> None:
        """窗口关闭仍待复核的批次置为 expired 并释放配额，不挤占新窗口。"""
        now = self._now().isoformat()
        rows = self._conn.execute(
            "SELECT d.demand_id, d.window_id, d.quantity FROM demands d"
            " JOIN windows w ON d.window_id = w.window_id"
            " WHERE d.state = ? AND w.close_at <= ?",
            (PENDING, now),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE demands SET state = 'expired', reason = '窗口已关闭，批次逾期',"
                " version = version + 1, updated_at = ? WHERE demand_id = ?",
                (now, row["demand_id"]),
            )
            self._conn.execute(
                "UPDATE windows SET reserved = reserved - ? WHERE window_id = ?",
                (row["quantity"], row["window_id"]),
            )
