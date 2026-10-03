import os
import tempfile
import unittest
from datetime import datetime

from app.contracts import Request
from app.service import RouteControlService

DAY = "2026-10-04"
OPEN = "2026-10-03T08:00:00"
CLOSE = "2026-10-03T20:00:00"


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = [datetime(2026, 10, 3, 9, 0, 0)]
        self.service = self._make_service()
        self.service.handle(Request("sched", "open_window", {
            "window_id": "W1", "route_id": "G102", "service_date": DAY,
            "open_at": OPEN, "close_at": CLOSE, "capacity": 10,
        }, "w-1"))

    def tearDown(self):
        self.service.close()

    def _make_service(self, db_path=":memory:"):
        return RouteControlService(
            db_path=db_path,
            reviewers={"lead"}, schedulers={"sched"},
            now=lambda: self.clock[0],
        )

    def _submit(self, request_id, quantity=4, city="杭州"):
        return self.service.handle(Request("city-ops", "submit", {
            "city": city, "route_id": "G102", "service_date": DAY, "quantity": quantity,
        }, request_id))

    def test_submit_locks_quota_and_waits_review(self):
        result = self._submit("r-1")
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, "pending_review")
        self.assertEqual(result.data["remaining"], 6)

    def test_retry_does_not_consume_quota_twice(self):
        first = self._submit("r-1")
        second = self._submit("r-1")
        self.assertEqual(first, second)
        self.assertEqual(second.data["remaining"], 6)
        # 重试未占配额：余量仍够另一个 6 辆的申请
        self.assertTrue(self._submit("r-2", quantity=6).accepted)

    def test_same_request_id_with_different_payload_conflicts(self):
        self._submit("r-1")
        conflict = self._submit("r-1", quantity=5)
        self.assertFalse(conflict.accepted)
        self.assertEqual(conflict.state, "conflict")

    def test_capacity_exceeded_is_rejected(self):
        result = self._submit("r-1", quantity=11)
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "capacity_exceeded")

    def test_submit_outside_window_is_rejected(self):
        self.clock[0] = datetime(2026, 10, 3, 21, 0, 0)
        result = self._submit("r-1")
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "no_window")

    def test_approve_records_decider_and_reason(self):
        self._submit("r-1")
        result = self.service.handle(Request(
            "lead", "approve", {"demand_id": "r-1", "reason": "高峰保障"}, "a-1"))
        self.assertTrue(result.accepted)
        query = self.service.handle(Request("city-ops", "query", {"demand_id": "r-1"}, "q-1"))
        self.assertEqual(query.data["state"], "approved")
        self.assertEqual(query.data["reason"], "高峰保障")
        self.assertEqual(query.data["decided_by"], "lead")

    def test_review_requires_permission(self):
        self._submit("r-1")
        result = self.service.handle(Request("city-ops", "approve", {"demand_id": "r-1"}, "a-1"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "forbidden")

    def test_reject_releases_quota(self):
        self._submit("r-1", quantity=8)
        self.service.handle(Request("lead", "reject", {"demand_id": "r-1"}, "a-1"))
        self.assertTrue(self._submit("r-2", quantity=10).accepted)

    def test_overdue_batch_cannot_be_approved_nor_block_new_window(self):
        self._submit("r-1", quantity=8)
        self.clock[0] = datetime(2026, 10, 3, 21, 0, 0)  # W1 已关闭
        late = self.service.handle(Request("lead", "approve", {"demand_id": "r-1"}, "a-1"))
        self.assertFalse(late.accepted)
        self.assertEqual(late.state, "expired")
        # 新窗口容量不被逾期批次挤占
        self.service.handle(Request("sched", "open_window", {
            "window_id": "W2", "route_id": "G102", "service_date": DAY,
            "open_at": "2026-10-03T20:30:00", "close_at": "2026-10-03T23:00:00", "capacity": 10,
        }, "w-2"))
        self.assertTrue(self._submit("r-2", quantity=10).accepted)

    def test_restart_resumes_pending_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "dispatch.db")
            service = self._make_service(db)
            service.handle(Request("sched", "open_window", {
                "window_id": "W1", "route_id": "G102", "service_date": DAY,
                "open_at": OPEN, "close_at": CLOSE, "capacity": 10,
            }, "w-1"))
            service.handle(Request("city-ops", "submit", {
                "city": "杭州", "route_id": "G102", "service_date": DAY, "quantity": 4,
            }, "r-1"))
            service.close()  # 模拟服务重启

            resumed = self._make_service(db)
            retry = resumed.handle(Request("city-ops", "submit", {
                "city": "杭州", "route_id": "G102", "service_date": DAY, "quantity": 4,
            }, "r-1"))
            self.assertEqual(retry.data["remaining"], 6)  # 配额未被重复占用
            approved = resumed.handle(Request("lead", "approve", {"demand_id": "r-1"}, "a-1"))
            self.assertTrue(approved.accepted)
            query = resumed.handle(Request("city-ops", "query", {"demand_id": "r-1"}, "q-1"))
            self.assertEqual(query.data["state"], "approved")
            self.assertEqual(query.data["decided_by"], "lead")
            resumed.close()


if __name__ == "__main__":
    unittest.main()
