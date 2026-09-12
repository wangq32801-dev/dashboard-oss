from pathlib import Path
import json
import subprocess
import unittest


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = Path(__file__).resolve().parents[1]


class Batch8ContractTests(unittest.TestCase):
    def test_expensive_routes_have_bounded_rate_limits(self):
        source = (ROOT / "dashboard-server.py").read_text(encoding="utf-8")
        for route in (
            "/api/butler/vision",
            "/api/butler/stream",
            "/api/advisor/stream",
            "/api/munger/advisor",
            "/api/ai/draft",
            "/api/ai/oracle/refresh",
            "/api/review/draft/refresh",
            "/api/recovery/restore",
        ):
            self.assertIn(route, source)
        self.assertIn("_rate_limit_retry", source)
        self.assertIn('status=429', source)
        self.assertIn('"Retry-After"', source)
        self.assertIn("请求过于频繁，请稍后重试", source)
        self.assertIn('Cross-Origin-Resource-Policy", "same-origin"', source)
        self.assertIn('"\\r" not in value and "\\n" not in value', source)

    def test_vision_input_boundary_is_strict(self):
        source = (ROOT / "dashboard-server.py").read_text(encoding="utf-8")
        self.assertIn("allowed_mimes =", source)
        for mime in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            self.assertIn(mime, source)
        self.assertIn("base64.b64decode(img, validate=True)", source)
        self.assertIn("不支持的图片类型", source)
        self.assertIn("图片编码无效", source)

    def test_private_state_stays_owner_only_after_atomic_replace(self):
        source = (ROOT / "dashboard-server.py").read_text(encoding="utf-8")
        self.assertIn("def _restrict_private_mode", source)
        self.assertIn("os.chmod(path, 0o600)", source)
        self.assertGreaterEqual(source.count("_restrict_private_mode("), 4)

    def test_rate_limit_is_process_local_and_bounded(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("dashboard_server_batch8", ROOT / "dashboard-server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._rate_windows.clear()
        path = "/api/butler/vision"
        for _ in range(6):
            self.assertEqual(module._rate_limit_retry(path, "batch8-test"), 0)
        self.assertGreaterEqual(module._rate_limit_retry(path, "batch8-test"), 1)
        # A different client remains independent; regular reads are not throttled.
        self.assertEqual(module._rate_limit_retry(path, "batch8-other"), 0)
        self.assertEqual(module._rate_limit_retry("/api/dashboard-data", "batch8-test"), 0)

    def test_recovery_drill_restores_all_nine_domains_in_temp_dir(self):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "recovery-drill.py")],
            cwd=ROOT, check=True, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload, {"success": True, "domains": 9, "restored": 9, "temporary_only": True})

    def test_mirror_rebuild_drill_rebuilds_three_domains_in_temp_dir(self):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "mirror-rebuild-drill.py")],
            cwd=ROOT, check=True, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload, {
            "success": True, "mirrors": 3, "rebuilt": 3,
            "drift": 0, "temporary_only": True,
        })


if __name__ == "__main__":
    unittest.main()
