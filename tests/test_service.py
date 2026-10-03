import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from app.contracts import Request
from app.service import RouteControlService

ROUTE = "R-NIGHT-1"
OP_DATE = "2026-10-04"


def req(actor, action, payload, request_id, ts=None):
    if ts is not None:
        payload = {**payload, "ts": ts}
    return Request(actor, action, payload, request_id,
                   datetime(2026, 1, 1, tzinfo=timezone.utc))


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.svc = RouteControlService(":memory:")
        self._configure()

    def tearDown(self):
        self.svc.close()

    def _configure(self):
        self.svc.handle(req("ops", "configure_route", {
            "route_code": ROUTE, "route_name": "中秋夜加班线", "total_seats": 60}, "cfg-route"))
        # 第一窗口 18:00-20:00，第二窗口 21:00-23:00（东八区）
        self.svc.handle(req("ops", "configure_window", {
            "route_code": ROUTE, "op_date": OP_DATE, "batch_no": 1,
            "opens_at": "2026-10-04T18:00:00+08:00",
            "closes_at": "2026-10-04T20:00:00+08:00"}, "cfg-win-1"))
        self.svc.handle(req("ops", "configure_window", {
            "route_code": ROUTE, "op_date": OP_DATE, "batch_no": 2,
            "opens_at": "2026-10-04T21:00:00+08:00",
            "closes_at": "2026-10-04T23:00:00+08:00"}, "cfg-win-2"))
        self.svc.handle(req("ops", "grant_reviewer", {
            "route_code": ROUTE, "username": "dispatcher-li"}, "grant-li"))

    def submit(self, city, seats, rid, ts, batch=None):
        payload = {"route_code": ROUTE, "op_date": OP_DATE,
                   "city": city, "seats": seats}
        if batch is not None:
            payload["batch_no"] = batch
        return self.svc.handle(req(city, "submit", payload, rid, ts))

    # ---- 受理与窗口锁定 -------------------------------------------------

    def test_submit_locks_into_open_window(self):
        r = self.submit("A城", 20, "sub-a", "2026-10-04T19:00:00+08:00")
        self.assertTrue(r.accepted)
        self.assertEqual(r.state, "pending")
        self.assertEqual(r.data["batch_no"], 1)

    def test_expired_batch_cannot_enter_new_window(self):
        # 19:30 的申请按时间应锁定批次 1；窗口关闭后（20:30）重试或补录，
        # 即使批次 2 已经开放，也必须明确拒绝。
        late = self.submit("A城", 20, "sub-late", "2026-10-04T20:30:00+08:00")
        self.assertFalse(late.accepted)
        self.assertEqual(late.error_code, "window_closed")
        # 显式指定逾期批次同样被拒
        late2 = self.submit("A城", 20, "sub-late2", "2026-10-04T21:30:00+08:00", batch=1)
        self.assertFalse(late2.accepted)
        self.assertEqual(late2.error_code, "window_closed")
        # 新窗口内的正常申请不受影响
        fresh = self.submit("B城", 20, "sub-fresh", "2026-10-04T21:30:00+08:00")
        self.assertTrue(fresh.accepted)
        self.assertEqual(fresh.data["batch_no"], 2)

    def test_old_batch_never_consumes_new_window_capacity(self):
        # 批次 1 批准 60 个座位（占满），批次 2 的容量必须独立计算
        r = self.submit("A城", 60, "sub-full", "2026-10-04T19:00:00+08:00")
        key = r.data["request_key"]
        appr = self.svc.handle(req("dispatcher-li", "approve",
                                   {"request_key": key}, "appr-full",
                                   "2026-10-04T19:10:00+08:00"))
        self.assertTrue(appr.accepted)
        r2 = self.submit("C城", 60, "sub-b2", "2026-10-04T21:30:00+08:00")
        appr2 = self.svc.handle(req("dispatcher-li", "approve",
                                    {"request_key": r2.data["request_key"]},
                                    "appr-b2", "2026-10-04T21:40:00+08:00"))
        self.assertTrue(appr2.accepted)

    # ---- 幂等与容量 -----------------------------------------------------

    def test_submit_retry_does_not_duplicate(self):
        r1 = self.submit("A城", 20, "dup-id", "2026-10-04T19:00:00+08:00")
        r2 = self.submit("A城", 20, "dup-id", "2026-10-04T19:00:00+08:00")
        self.assertTrue(r2.data.get("replayed"))
        self.assertEqual(r1.data["request_key"], r2.data["request_key"])

    def test_approve_retry_does_not_consume_quota_twice(self):
        key = self.submit("A城", 40, "sub-x", "2026-10-04T19:00:00+08:00").data["request_key"]
        command = req("dispatcher-li", "approve", {"request_key": key},
                      "appr-x", "2026-10-04T19:10:00+08:00")
        first = self.svc.handle(command)
        second = self.svc.handle(command)
        self.assertTrue(first.accepted)
        self.assertTrue(second.data.get("replayed"))
        # 批次 1 仍剩 20，而不是因重试被扣成 -20
        q = self.svc.handle(req("ops", "query",
                                {"route_code": ROUTE, "op_date": OP_DATE},
                                "q1", "2026-10-04T19:30:00+08:00"))
        w1 = next(w for w in q.data["windows"] if w["batch_no"] == 1)
        self.assertEqual(w1["used_seats"], 40)
        self.assertEqual(w1["remaining_seats"], 20)

    def test_capacity_enforced_across_cities(self):
        k1 = self.submit("A城", 40, "sub-a40", "2026-10-04T19:00:00+08:00").data["request_key"]
        k2 = self.submit("B城", 30, "sub-b30", "2026-10-04T19:01:00+08:00").data["request_key"]
        self.assertTrue(self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": k1}, "a1", "2026-10-04T19:10:00+08:00")).accepted)
        over = self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": k2}, "a2", "2026-10-04T19:11:00+08:00"))
        self.assertFalse(over.accepted)
        self.assertEqual(over.error_code, "capacity_exceeded")
        self.assertEqual(over.data["remaining_seats"], 20)
        # 容量不足没有改变申请状态，也没有留下任何占用
        self.assertEqual(self.svc.handle(req("ops", "query",
            {"route_code": ROUTE, "op_date": OP_DATE, "city": "B城"},
            "q2", "2026-10-04T19:12:00+08:00")).data["decisions"][0]["status"], "pending")

    def test_reject_occupies_nothing_and_revoke_releases(self):
        k1 = self.submit("A城", 55, "sub-55", "2026-10-04T19:00:00+08:00").data["request_key"]
        rej = self.svc.handle(req("dispatcher-li", "reject",
            {"request_key": k1, "reason": "材料不全"}, "rej-1",
            "2026-10-04T19:05:00+08:00"))
        self.assertTrue(rej.accepted)
        k2 = self.submit("B城", 60, "sub-60", "2026-10-04T19:20:00+08:00").data["request_key"]
        appr = self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": k2}, "appr-60", "2026-10-04T19:21:00+08:00"))
        self.assertTrue(appr.accepted)  # 驳回的 55 座没有占用配额

        # 撤销后容量释放，另一笔申请可以顶上
        revoked = self.svc.handle(req("dispatcher-li", "revoke",
            {"request_key": k2, "reason": "B城取消加班"}, "rev-1",
            "2026-10-04T19:30:00+08:00"))
        self.assertTrue(revoked.accepted)
        k3 = self.submit("C城", 60, "sub-c60", "2026-10-04T19:35:00+08:00").data["request_key"]
        self.assertTrue(self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": k3}, "appr-c", "2026-10-04T19:36:00+08:00")).accepted)

    # ---- 权限与版本 -----------------------------------------------------

    def test_only_authorized_reviewer_may_act(self):
        key = self.submit("A城", 10, "sub-p", "2026-10-04T19:00:00+08:00").data["request_key"]
        denied = self.svc.handle(req("city-a", "approve",
            {"request_key": key}, "appr-denied", "2026-10-04T19:10:00+08:00"))
        self.assertFalse(denied.accepted)
        self.assertEqual(denied.error_code, "forbidden")

    def test_stale_version_approval_rejected(self):
        key = self.submit("A城", 10, "sub-v", "2026-10-04T19:00:00+08:00").data["request_key"]
        # 城市修订需求，版本升到 2
        amend = self.submit("A城", 12, "sub-v2", "2026-10-04T19:05:00+08:00")
        self.assertEqual(amend.data["version"], 2)
        # 复核人手里还是基于版本 1 的批复单
        stale = self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": key, "expected_version": 1}, "appr-stale",
            "2026-10-04T19:06:00+08:00"))
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.error_code, "version_conflict")
        # 按最新版本批复成功
        ok = self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": key, "expected_version": 2, "reason": "确认加车"},
            "appr-fresh", "2026-10-04T19:07:00+08:00"))
        self.assertTrue(ok.accepted)
        self.assertEqual(ok.data["version"], 3)

    def test_different_command_same_idempotency_key_rejected(self):
        self.submit("A城", 10, "same-id", "2026-10-04T19:00:00+08:00")
        clash = self.submit("A城", 11, "same-id", "2026-10-04T19:01:00+08:00")
        self.assertFalse(clash.accepted)
        self.assertEqual(clash.error_code, "version_conflict")

    # ---- 查询：有效决定 / 形成原因 / 责任人 ------------------------------

    def test_query_shows_effective_decision_history_and_owner(self):
        key = self.submit("A城", 20, "sub-q", "2026-10-04T19:00:00+08:00").data["request_key"]
        self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": key, "reason": "中秋首夜保障，准予加车"},
            "appr-q", "2026-10-04T19:15:00+08:00"))
        q = self.svc.handle(req("ops", "query",
            {"route_code": ROUTE, "op_date": OP_DATE, "city": "A城"},
            "q-all", "2026-10-04T19:20:00+08:00"))
        self.assertTrue(q.accepted)
        d = q.data["decisions"][0]
        self.assertTrue(d["effective"])
        self.assertEqual(d["status"], "approved")
        self.assertEqual(d["submitted_by"], "A城")
        self.assertEqual(d["reviewed_by"], "dispatcher-li")  # 责任人
        self.assertEqual(d["review_reason"], "中秋首夜保障，准予加车")
        types = [e["type"] for e in d["history"]]
        self.assertEqual(types, ["submitted", "approved"])
        self.assertEqual(d["history"][-1]["actor"], "dispatcher-li")
        # 窗口实时状态
        w1 = next(w for w in q.data["windows"] if w["batch_no"] == 1)
        self.assertEqual(w1["phase"], "open")
        self.assertEqual(w1["remaining_seats"], 40)

    def test_resubmit_after_revoke_and_reject(self):
        key = self.submit("A城", 60, "sub-rv", "2026-10-04T19:00:00+08:00").data["request_key"]
        self.assertTrue(self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": key}, "appr-rv", "2026-10-04T19:05:00+08:00")).accepted)
        self.assertTrue(self.svc.handle(req("dispatcher-li", "revoke",
            {"request_key": key, "reason": "城市取消"}, "rv", "2026-10-04T19:10:00+08:00")).accepted)
        # 撤销后城市重新提交（新 request_id），历史保留、决定失效
        again = self.submit("A城", 20, "sub-again", "2026-10-04T19:20:00+08:00")
        self.assertTrue(again.accepted)
        self.assertEqual(again.state, "pending")
        self.assertEqual(again.data["version"], 4)
        ok = self.svc.handle(req("dispatcher-li", "approve",
            {"request_key": key}, "appr-again", "2026-10-04T19:21:00+08:00"))
        self.assertTrue(ok.accepted)
        # 占用 = 旧批准 60 - 撤销 60 + 新批准 20 = 20
        q = self.svc.handle(req("ops", "query",
            {"route_code": ROUTE, "op_date": OP_DATE, "city": "A城"},
            "q-rv", "2026-10-04T19:25:00+08:00"))
        self.assertEqual(q.data["windows"][0]["used_seats"], 20)
        types = [e["type"] for e in q.data["decisions"][0]["history"]]
        self.assertEqual(types, ["submitted", "approved", "revoked",
                                 "resubmitted", "approved"])

        # 仍有效的批准不能被新提交覆盖
        cover = self.submit("A城", 5, "sub-cover", "2026-10-04T19:30:00+08:00")
        self.assertFalse(cover.accepted)
        self.assertEqual(cover.error_code, "already_reviewed")

    # ---- 并发不超售 -----------------------------------------------------

    def test_concurrent_approvals_never_oversell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dispatch.db")
            setup = RouteControlService(path)
            setup.handle(req("ops", "configure_route", {
                "route_code": ROUTE, "total_seats": 60}, "c-route"))
            setup.handle(req("ops", "configure_window", {
                "route_code": ROUTE, "op_date": OP_DATE, "batch_no": 1,
                "opens_at": "2026-10-04T18:00:00+08:00",
                "closes_at": "2026-10-04T20:00:00+08:00"}, "c-win"))
            setup.handle(req("ops", "grant_reviewer", {
                "route_code": ROUTE, "username": "dispatcher-li"}, "c-grant"))
            setup.close()

            def isolated(cmd_id, city, seats):
                svc = RouteControlService(path)
                try:
                    sub = svc.handle(req(city, "submit", {
                        "route_code": ROUTE, "op_date": OP_DATE,
                        "city": city, "seats": seats}, f"sub-{cmd_id}",
                        "2026-10-04T19:00:00+08:00"))
                    appr = svc.handle(req("dispatcher-li", "approve",
                        {"request_key": sub.data["request_key"]},
                        f"appr-{cmd_id}", "2026-10-04T19:10:00+08:00"))
                    return appr.accepted
                finally:
                    svc.close()

            results = []
            lock = threading.Lock()

            def worker(cid):
                ok = isolated(cid, f"城市{cid}", 40)
                with lock:
                    results.append(ok)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(sum(results), 1)  # 60 座只够一笔 40
            svc = RouteControlService(path)
            q = svc.handle(req("ops", "query",
                {"route_code": ROUTE, "op_date": OP_DATE}, "q-conc",
                "2026-10-04T19:30:00+08:00"))
            w1 = next(w for w in q.data["windows"] if w["batch_no"] == 1)
            self.assertEqual(w1["used_seats"], 40)
            svc.close()

    # ---- 重启续办 / 中断回放 ---------------------------------------------

    def test_restart_resumes_from_last_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dispatch.db")
            svc = RouteControlService(path)
            svc.handle(req("ops", "configure_route", {
                "route_code": ROUTE, "total_seats": 60}, "r-route"))
            svc.handle(req("ops", "configure_window", {
                "route_code": ROUTE, "op_date": OP_DATE, "batch_no": 1,
                "opens_at": "2026-10-04T18:00:00+08:00",
                "closes_at": "2026-10-04T20:00:00+08:00"}, "r-win"))
            svc.handle(req("ops", "grant_reviewer", {
                "route_code": ROUTE, "username": "dispatcher-li"}, "r-grant"))
            sub = svc.handle(req("A城", "submit", {
                "route_code": ROUTE, "op_date": OP_DATE,
                "city": "A城", "seats": 25}, "persist-sub",
                "2026-10-04T19:00:00+08:00"))
            key = sub.data["request_key"]
            svc.handle(req("dispatcher-li", "approve",
                {"request_key": key}, "persist-appr",
                "2026-10-04T19:10:00+08:00"))
            svc.close()

            # 模拟服务重启 + 调用方网络重试：同一批准命令重放
            svc2 = RouteControlService(path)
            replay = svc2.handle(req("dispatcher-li", "approve",
                {"request_key": key}, "persist-appr",
                "2026-10-04T19:10:00+08:00"))
            self.assertTrue(replay.data.get("replayed"))
            self.assertEqual(replay.state, "approved")
            q = svc2.handle(req("ops", "query",
                {"route_code": ROUTE, "op_date": OP_DATE}, "persist-q",
                "2026-10-04T19:30:00+08:00"))
            w1 = next(w for w in q.data["windows"] if w["batch_no"] == 1)
            self.assertEqual(w1["used_seats"], 25)  # 未重复扣减
            svc2.close()

            raw = sqlite3.connect(path)
            # 决定事件与责任人均已持久化
            kinds = [r[0] for r in raw.execute(
                "SELECT event_type FROM events ORDER BY id")]
            self.assertEqual(kinds, ["submitted", "approved"])
            raw.close()


class CliCase(unittest.TestCase):
    def test_batch_via_api_run(self):
        from app import api
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cli.db")
            requests = [
                {"actor": "ops", "action": "configure_route", "request_id": "c1",
                 "payload": {"route_code": ROUTE, "total_seats": 10}},
                {"actor": "ops", "action": "configure_window", "request_id": "c2",
                 "payload": {"route_code": ROUTE, "op_date": OP_DATE, "batch_no": 1,
                             "opens_at": "2026-10-04T08:00:00+00:00",
                             "closes_at": "2026-10-04T16:00:00+00:00"}},
                {"actor": "ops", "action": "grant_reviewer", "request_id": "c3",
                 "payload": {"route_code": ROUTE, "username": "dispatcher-li"}},
                {"actor": "A城", "action": "submit", "request_id": "c4",
                 "payload": {"route_code": ROUTE, "op_date": OP_DATE,
                             "city": "A城", "seats": 10,
                             "ts": "2026-10-04T10:00:00+00:00"}},
            ]
            results, ok, batch = api.run(path, json.dumps({"requests": requests},
                                                           ensure_ascii=False))
            self.assertTrue(batch)
            self.assertTrue(ok)
            self.assertEqual(results[3]["data"]["batch_no"], 1)


if __name__ == "__main__":
    unittest.main()
