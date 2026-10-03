"""运力决策领域服务。

处理流程（对应业务诉求）：

1. 城市按运营日提交线路加班需求（``submit``）——系统根据命令时间
   把申请锁定到 *唯一* 的时间窗口批次；窗口关闭后的逾期申请一律拒绝，
   不会自动落入或挤占后续新窗口。
2. 系统在窗口锁定后做容量校验（``approve``）——批准占用在写事务内
   实时汇总并比较线路总配额，跨城市并发也不会超售；同一 request_id
   重试只回放原结果，绝不二次占用配额。
3. 只有被授权的复核人可以批准/驳回/撤销；每次复核留下决定、原因与
   责任人。申请版本号单调递增，携带旧版本号的批复会被拒绝，
   现场始终能确认“哪次批复仍然有效”。
4. ``query`` 直接返回当前有效决定、完整形成原因（事件流）与责任人，
   以及各窗口剩余容量。所有状态落 SQLite，进程重启或审批中断后，
   凭同一 request_id 即可从原进度继续。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Union

from .contracts import (
    ERR_ALREADY_REVIEWED,
    ERR_CAPACITY,
    ERR_FORBIDDEN,
    ERR_NOT_FOUND,
    ERR_NOT_PENDING,
    ERR_ROUTE_UNKNOWN,
    ERR_UNKNOWN_ACTION,
    ERR_VALIDATION,
    ERR_VERSION_CONFLICT,
    ERR_WINDOW_CLOSED,
    ERR_WINDOW_NOT_FOUND,
    Request,
    Result,
    now_utc,
    validate_request,
)
from .storage import Storage, parse_iso as storage_parse_iso

REQUEST_KEY_FMT = "{route}|{op_date}|{batch}|{city}"


class RouteControlService:
    def __init__(self, storage: Union[Storage, str, None] = None,
                 clock: Callable[[], datetime] = now_utc) -> None:
        if isinstance(storage, Storage):
            self._storage = storage
            self._owns_storage = False
        else:
            self._storage = Storage(storage or ":memory:")
            self._owns_storage = True
        self._clock = clock

    def close(self) -> None:
        if self._owns_storage:
            self._storage.close()

    # ---- 入口 -----------------------------------------------------------

    def handle(self, request: Request) -> Result:
        validate_request(request)
        handler = {
            "configure_route": self._configure_route,
            "configure_window": self._configure_window,
            "grant_reviewer": self._grant_reviewer,
            "submit": self._submit,
            "approve": self._review,
            "reject": self._review,
            "revoke": self._revoke,
        }.get(request.action)

        # 查询只读，实时反映当前有效决定，不做命令去重。
        if request.action == "query":
            return self._query(request)

        if handler is None:
            return Result(False, "rejected", f"未知动作: {request.action}",
                          error_code=ERR_UNKNOWN_ACTION)

        st = self._storage
        with st.transaction() as conn:
            row = st.get_command(conn, request.request_id)
            if row is not None:
                same_command = (
                    row["actor"] == request.actor
                    and row["action"] == request.action
                    and json.loads(row["payload"]) == request.payload
                )
                if not same_command:
                    return Result(
                        False, "rejected",
                        "幂等键已被内容不同的命令使用",
                        data={"original_request_id": request.request_id},
                        error_code=ERR_VERSION_CONFLICT,
                    )
                replay = json.loads(row["result_json"])
                cached = Result(
                    replay["accepted"], replay["state"], replay["message"],
                    dict(replay.get("data", {})), replay.get("error_code"),
                )
                cached.data["replayed"] = True
                return cached
            result = handler(conn, request)
            st.save_command_result(
                conn, request.request_id, request.actor, request.action,
                request.payload, result.to_dict(),
            )
            return result

    # ---- 配置类动作 -----------------------------------------------------

    def _configure_route(self, conn, request: Request) -> Result:
        p = request.payload
        try:
            route_code = str(p["route_code"]).strip()
            total_seats = int(p["total_seats"])
        except (KeyError, TypeError, ValueError):
            return self._invalid("route_code 与非负整数 total_seats 为必填")
        if not route_code or total_seats < 0:
            return self._invalid("route_code 不能为空，total_seats 不能为负")
        route_name = str(p.get("route_name") or route_code).strip()
        self._storage.upsert_route(conn, route_code, route_name, total_seats)
        return Result(True, "configured", f"线路 {route_code} 总配额 {total_seats}",
                      {"route_code": route_code, "total_seats": total_seats})

    def _configure_window(self, conn, request: Request) -> Result:
        p = request.payload
        try:
            route_code = str(p["route_code"]).strip()
            op_date = self._op_date(p["op_date"])
            batch_no = int(p["batch_no"])
            opens_at = self._dt(p["opens_at"])
            closes_at = self._dt(p["closes_at"])
        except (KeyError, TypeError, ValueError) as exc:
            return self._invalid(f"窗口参数不完整或格式错误: {exc}")
        if not self._storage.get_route(conn, route_code):
            return self._route_unknown(route_code)
        if not (opens_at < closes_at):
            return self._invalid("窗口开启时间必须早于关闭时间")
        if self._storage.get_window(conn, route_code, op_date, batch_no):
            return Result(
                False, "window_locked",
                f"批次 {batch_no} 窗口已存在，时间窗口锁定后不可修改",
                {"route_code": route_code, "op_date": op_date, "batch_no": batch_no},
                error_code=ERR_VERSION_CONFLICT,
            )
        self._storage.add_window(conn, route_code, op_date, batch_no, opens_at, closes_at)
        return Result(
            True, "configured",
            f"窗口已锁定: {op_date} 批次 {batch_no} "
            f"{opens_at.isoformat()} ~ {closes_at.isoformat()}",
            {"route_code": route_code, "op_date": op_date, "batch_no": batch_no,
             "opens_at": opens_at.isoformat(), "closes_at": closes_at.isoformat()},
        )

    def _grant_reviewer(self, conn, request: Request) -> Result:
        p = request.payload
        route_code = str(p.get("route_code", "")).strip()
        username = str(p.get("username", "")).strip()
        if not route_code or not username:
            return self._invalid("route_code 与 username 为必填")
        if not self._storage.get_route(conn, route_code):
            return self._route_unknown(route_code)
        self._storage.grant_reviewer(conn, route_code, username)
        return Result(True, "configured", f"已授权 {username} 复核线路 {route_code}",
                      {"route_code": route_code, "username": username})

    # ---- 提交 -----------------------------------------------------------

    def _submit(self, conn, request: Request) -> Result:
        p = request.payload
        st = self._storage
        try:
            route_code = str(p["route_code"]).strip()
            op_date = self._op_date(p["op_date"])
            city = str(p["city"]).strip()
            seats = int(p["seats"])
            batch_no = p.get("batch_no")
            batch_no = None if batch_no is None else int(batch_no)
        except (KeyError, TypeError, ValueError) as exc:
            return self._invalid(f"申请参数不完整或格式错误: {exc}")
        if not route_code or not city or seats <= 0:
            return self._invalid("route_code、city 不能为空，seats 必须为正整数")

        route = st.get_route(conn, route_code)
        if not route:
            return self._route_unknown(route_code)

        ts = request.ts
        window_result = self._resolve_window(conn, route_code, op_date, batch_no, ts)
        if isinstance(window_result, Result):
            return window_result
        window = window_result
        locked_batch = int(window["batch_no"])

        key = REQUEST_KEY_FMT.format(route=route_code, op_date=op_date,
                                    batch=locked_batch, city=city)
        existing = st.get_request(conn, key)
        if existing is not None:
            if existing["status"] == "approved":
                return Result(
                    False, existing["status"],
                    f"该城市 {op_date} 批次 {locked_batch} 的批准决定仍然有效，"
                    "不能被新提交覆盖；如需调整请先由复核人撤销",
                    self._request_summary(existing),
                    error_code=ERR_ALREADY_REVIEWED,
                )
            if existing["status"] in ("rejected", "revoked"):
                # 失效决定可以重新提交：同一申请单回到 pending，历史保留，
                # 版本递增。撤销已释放的配额不影响再次批准的占用口径。
                old_seats = int(existing["seats"])
                st.reopen_request(conn, key, seats, request.actor, ts)
                st.add_event(conn, key, "resubmitted", seats, request.actor, ts,
                             f"原决定 {existing['status']} 后重新提交"
                             f"（座位数 {old_seats} -> {seats}）")
                row = st.get_request(conn, key)
                return Result(
                    True, "pending",
                    f"原 {existing['status']} 决定已失效，重新提交已受理（版本 "
                    f"{row['version']}），等待复核",
                    self._request_summary(row),
                )
            old_seats = int(existing["seats"])
            if old_seats == seats:
                return Result(
                    True, "pending",
                    "相同内容的申请已存在，仍在等待复核，未重复占用配额",
                    self._request_summary(existing),
                )
            # 待复核期间允许修订需求：版本递增并留痕，此时尚未占用任何配额。
            st.update_pending_seats(conn, key, seats, request.actor, ts,
                                    f"座位数修订 {old_seats} -> {seats}")
            st.add_event(conn, key, "amended", seats, request.actor, ts,
                         f"座位数修订 {old_seats} -> {seats}")
            row = st.get_request(conn, key)
            return Result(
                True, "pending",
                f"需求已修订（座位数 {old_seats} -> {seats}），版本 {row['version']}，等待复核",
                self._request_summary(row),
            )

        st.insert_request(conn, key, route_code, op_date, locked_batch, city,
                          seats, request.actor, ts)
        st.add_event(conn, key, "submitted", seats, request.actor, ts,
                     f"{city} 提交 {op_date} 批次 {locked_batch} 加班需求")
        row = st.get_request(conn, key)
        return Result(
            True, "pending",
            f"已受理并锁定到 {op_date} 批次 {locked_batch} 窗口，等待有权限的复核人处理",
            self._request_summary(row),
        )

    # ---- 复核 -----------------------------------------------------------

    def _review(self, conn, request: Request) -> Result:
        approve = request.action == "approve"
        p = request.payload
        st = self._storage
        key = str(p.get("request_key", "")).strip()
        if not key:
            return self._invalid("request_key 为必填")
        row = st.get_request(conn, key)
        if row is None:
            return Result(False, "not_found", f"找不到申请: {key}",
                          error_code=ERR_NOT_FOUND)

        route_code = row["route_code"]
        if not st.is_reviewer(conn, route_code, request.actor):
            return Result(
                False, row["status"],
                f"{request.actor} 无权复核线路 {route_code}，请联系调度席授权",
                self._request_summary(row),
                error_code=ERR_FORBIDDEN,
            )
        if row["status"] != "pending":
            return Result(
                False, row["status"],
                f"申请已处于 {row['status']} 状态，不能重复复核",
                self._request_summary(row),
                error_code=ERR_NOT_PENDING,
            )

        expected = p.get("expected_version")
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                return self._invalid("expected_version 必须是整数")
            if expected != int(row["version"]):
                return Result(
                    False, "version_conflict",
                    f"批复基于版本 {expected}，但申请当前为版本 {row['version']}，"
                    "请按最新决定重新操作",
                    self._request_summary(row),
                    error_code=ERR_VERSION_CONFLICT,
                )

        reason = str(p.get("reason", "")).strip()
        ts = request.ts
        seats = int(row["seats"])

        if approve:
            used = st.used_seats(conn, route_code, row["op_date"], int(row["batch_no"]))
            total = int(st.get_route(conn, route_code)["total_seats"])
            if used + seats > total:
                return Result(
                    False, "capacity_exceeded",
                    f"批次 {row['batch_no']} 剩余容量 {max(total - used, 0)}，"
                    f"本申请需要 {seats}，容量不足",
                    {**self._request_summary(row), "used_seats": used,
                     "total_seats": total, "remaining_seats": total - used},
                    error_code=ERR_CAPACITY,
                )
            new_status, event_type, default_reason = (
                "approved", "approved", "复核通过，占用线路配额")
            message = (f"已批准，占用 {seats} 个座位；"
                       f"批次 {row['batch_no']} 剩余 {total - used - seats}")
        else:
            new_status, event_type, default_reason = (
                "rejected", "rejected", "复核驳回，不占用配额")
            message = "已驳回，未占用配额"

        st.review_request(conn, key, new_status, request.actor, ts,
                          reason or default_reason)
        st.add_event(conn, key, event_type, seats, request.actor, ts,
                     reason or default_reason)
        row = st.get_request(conn, key)
        return Result(True, new_status, message, self._request_summary(row))

    def _revoke(self, conn, request: Request) -> Result:
        p = request.payload
        st = self._storage
        key = str(p.get("request_key", "")).strip()
        reason = str(p.get("reason", "")).strip()
        if not key:
            return self._invalid("request_key 为必填")
        row = st.get_request(conn, key)
        if row is None:
            return Result(False, "not_found", f"找不到申请: {key}",
                          error_code=ERR_NOT_FOUND)
        if not st.is_reviewer(conn, row["route_code"], request.actor):
            return Result(False, row["status"],
                          f"{request.actor} 无权操作线路 {row['route_code']}",
                          self._request_summary(row), error_code=ERR_FORBIDDEN)
        if row["status"] != "approved":
            return Result(
                False, row["status"],
                f"仅已批准的决定可以撤销，当前状态: {row['status']}",
                self._request_summary(row), error_code=ERR_NOT_PENDING)

        ts = request.ts
        seats = int(row["seats"])
        st.review_request(conn, key, "revoked", request.actor, ts,
                          reason or "撤销已批准决定，释放配额")
        st.add_event(conn, key, "revoked", seats, request.actor, ts,
                     reason or "撤销已批准决定，释放配额")
        row = st.get_request(conn, key)
        return Result(True, "revoked",
                      f"已撤销，释放 {seats} 个座位配额",
                      self._request_summary(row))

    # ---- 查询 -----------------------------------------------------------

    def _query(self, request: Request) -> Result:
        p = request.payload
        st = self._storage
        route_code = str(p.get("route_code", "")).strip()
        op_date_raw = p.get("op_date")
        city = str(p.get("city", "")).strip() or None
        if not route_code or not op_date_raw:
            return self._invalid("query 需要 route_code 与 op_date")
        try:
            op_date = self._op_date(op_date_raw)
        except (TypeError, ValueError) as exc:
            return self._invalid(str(exc))

        conn = st.conn
        route = st.get_route(conn, route_code)
        if not route:
            return self._route_unknown(route_code)
        ts = request.ts
        windows = []
        for w in st.list_windows(conn, route_code, op_date):
            opens = storage_parse_iso(w["opens_at"])
            closes = storage_parse_iso(w["closes_at"])
            used = st.used_seats(conn, route_code, op_date, int(w["batch_no"]))
            total = int(route["total_seats"])
            windows.append({
                "batch_no": int(w["batch_no"]),
                "opens_at": w["opens_at"],
                "closes_at": w["closes_at"],
                "phase": ("open" if opens <= ts < closes
                          else "before" if ts < opens else "closed"),
                "total_seats": total,
                "used_seats": used,
                "remaining_seats": total - used,
            })

        decisions = []
        for row in st.list_requests(conn, route_code, op_date):
            if city and row["city"] != city:
                continue
            events = [
                {"type": e["event_type"], "actor": e["actor"],
                 "seats": int(e["seats"]), "reason": e["reason"],
                 "occurred_at": e["occurred_at"]}
                for e in st.events_for(conn, row["request_key"])
            ]
            decisions.append({
                **self._request_summary(row),
                "effective": row["status"] == "approved",
                "submitted_by": row["submitted_by"],
                "submitted_at": row["submitted_at"],
                "reviewed_by": row["reviewed_by"],
                "reviewed_at": row["reviewed_at"],
                "review_reason": row["review_reason"],
                "history": events,
            })

        return Result(True, "queried", "当前有效决定与形成原因如下", {
            "route_code": route_code,
            "route_name": route["route_name"],
            "op_date": op_date,
            "queried_at": ts.isoformat(),
            "total_seats": int(route["total_seats"]),
            "windows": windows,
            "decisions": decisions,
        })

    # ---- 辅助 -----------------------------------------------------------

    def _resolve_window(self, conn, route_code, op_date, batch_no, ts):
        st = self._storage
        if batch_no is not None:
            window = st.get_window(conn, route_code, op_date, batch_no)
            if window is None:
                return Result(
                    False, "window_not_found",
                    f"{op_date} 批次 {batch_no} 的窗口不存在",
                    error_code=ERR_WINDOW_NOT_FOUND)
            opens = storage_parse_iso(window["opens_at"])
            closes = storage_parse_iso(window["closes_at"])
            if ts < opens:
                return Result(False, "window_closed",
                              f"批次 {batch_no} 窗口尚未开启（{opens.isoformat()}）",
                              error_code=ERR_WINDOW_CLOSED)
            if ts >= closes:
                # 逾期批次：明确拒绝，绝不改挂后续新窗口。
                return Result(
                    False, "window_closed",
                    f"批次 {batch_no} 窗口已于 {closes.isoformat()} 截止，"
                    "逾期申请不能补录或挤占新窗口",
                    error_code=ERR_WINDOW_CLOSED)
            return window

        windows = st.list_windows(conn, route_code, op_date)
        if not windows:
            return Result(False, "window_not_found",
                          f"{route_code} 在 {op_date} 尚未配置任何申请窗口",
                          error_code=ERR_WINDOW_NOT_FOUND)
        for window in windows:
            opens = storage_parse_iso(window["opens_at"])
            closes = storage_parse_iso(window["closes_at"])
            if opens <= ts < closes:
                return window
        last_close = max(storage_parse_iso(w["closes_at"]) for w in windows)
        if ts >= last_close:
            return Result(
                False, "window_closed",
                f"{op_date} 的全部申请窗口均已截止（最晚 {last_close.isoformat()}），"
                "逾期批次不能挤占新窗口",
                error_code=ERR_WINDOW_CLOSED)
        return Result(False, "window_closed",
                      "当前时间不在任何开放中的申请窗口内",
                      error_code=ERR_WINDOW_CLOSED)

    @staticmethod
    def _request_summary(row) -> dict[str, Any]:
        return {
            "request_key": row["request_key"],
            "route_code": row["route_code"],
            "op_date": row["op_date"],
            "batch_no": int(row["batch_no"]),
            "city": row["city"],
            "seats": int(row["seats"]),
            "status": row["status"],
            "version": int(row["version"]),
        }

    @staticmethod
    def _op_date(value) -> str:
        return date.fromisoformat(str(value).strip()).isoformat()

    @staticmethod
    def _dt(value) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return storage_parse_iso(str(value))

    @staticmethod
    def _invalid(message: str) -> Result:
        return Result(False, "rejected", message, error_code=ERR_VALIDATION)

    @staticmethod
    def _route_unknown(route_code: str) -> Result:
        return Result(False, "route_unknown",
                      f"线路未配置: {route_code}",
                      {"route_code": route_code}, error_code=ERR_ROUTE_UNKNOWN)
