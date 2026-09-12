"""Data authority registry and payload-free status contract tests."""
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


class DataAuthorityTests(unittest.TestCase):
    def test_registry_covers_core_domains_and_shadow_is_read_model(self):
        registry = dashboard.DATA_AUTHORITY_REGISTRY
        for key in ("tasks", "habits", "roles", "reviews", "dashboard_json", "health_raw", "ai_outcomes", "sqlite_shadow"):
            self.assertIn(key, registry)
            self.assertIn("authority", registry[key])
            self.assertIn("revision", registry[key])
        self.assertFalse(registry["health_raw"]["writable"])
        self.assertFalse(registry["sqlite_shadow"]["writable"])
        self.assertEqual(registry["sqlite_shadow"]["authority"], "derived read model")

    def test_status_is_counts_only_and_reports_conflict_domains(self):
        original = dashboard.SYNC_CONFLICT_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.SYNC_CONFLICT_FILE = os.path.join(td, "conflicts.json")
                with open(dashboard.SYNC_CONFLICT_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"items": [{"domain": "relations.json", "key": "id:x", "payload": "secret"}]}, fh)
                status = dashboard._data_authority_status()
                self.assertEqual(status["conflictCount"], 1)
                self.assertEqual(status["conflictDomains"], ["relations.json"])
                self.assertEqual(status["shadow"]["mode"], "read-model")
                self.assertNotIn("payload", json.dumps(status, ensure_ascii=False))
                self.assertNotIn("/", json.dumps(status, ensure_ascii=False))
        finally:
            dashboard.SYNC_CONFLICT_FILE = original


if __name__ == "__main__":
    unittest.main()
