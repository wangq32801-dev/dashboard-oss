import importlib.util
import json
import os
import tempfile
import unittest


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("dashboard_server", os.path.join(ROOT, "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class SyncConflictEndpointTests(unittest.TestCase):
    def test_report_is_bounded_and_payload_free(self):
        original = dashboard.SYNC_CONFLICT_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.SYNC_CONFLICT_FILE = os.path.join(td, "conflicts.json")
                with open(dashboard.SYNC_CONFLICT_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"count": 2, "updated_at": "now", "items": [
                        {"key": "id:x", "reason": "same-key-different-payload", "left_side": "mac", "right_side": "vps"},
                        {"key": "id:y", "payload": "must not be returned"},
                    ]}, fh)
                result = object.__new__(dashboard.DashboardHandler)._sync_conflicts()
                self.assertEqual(result["count"], 2)
                self.assertEqual(len(result["items"]), 2)
                self.assertEqual(result["items"][0]["key"], "id:x")
                self.assertNotIn("payload", result["items"][1])
        finally:
            dashboard.SYNC_CONFLICT_FILE = original

    def test_acknowledge_hides_only_current_report_item_without_payload(self):
        original_report = dashboard.SYNC_CONFLICT_FILE
        original_ack = dashboard.SYNC_CONFLICT_ACK_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.SYNC_CONFLICT_FILE = os.path.join(td, "conflicts.json")
                dashboard.SYNC_CONFLICT_ACK_FILE = os.path.join(td, "acks.json")
                item = {"key": "id:x", "reason": "same-key-different-payload", "fields": ["title"]}
                with open(dashboard.SYNC_CONFLICT_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"count": 1, "updated_at": "r1", "items": [item]}, fh)
                handler = object.__new__(dashboard.DashboardHandler)
                result = handler._ack_sync_conflict({"key": "id:x", "fields": ["title"]})
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 0)
                with open(dashboard.SYNC_CONFLICT_ACK_FILE, encoding="utf-8") as fh:
                    ack = json.load(fh)
                self.assertNotIn("payload", json.dumps(ack))
                # A newly generated report is a new incident and must reappear.
                with open(dashboard.SYNC_CONFLICT_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"count": 1, "updated_at": "r2", "items": [item]}, fh)
                self.assertEqual(handler._sync_conflicts()["count"], 1)
        finally:
            dashboard.SYNC_CONFLICT_FILE = original_report
            dashboard.SYNC_CONFLICT_ACK_FILE = original_ack


if __name__ == "__main__":
    unittest.main()
