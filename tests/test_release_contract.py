"""Static checks for the single release verification entry point."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-release.sh"
PERF = ROOT / "scripts" / "perf-baseline.mjs"


class ReleaseContractTests(unittest.TestCase):
    def test_release_script_has_all_gates(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for required in (
            "python3 -m unittest discover",
            "node tests/task-mutations.test.mjs",
            "node tests/write-coordinator.test.mjs",
            "tests/foldable-smoke.mjs",
            "scripts/recovery-drill.py",
            "git diff --check",
            "发布前验收失败，禁止发布",
        ):
            self.assertIn(required, text)
        self.assertNotIn("/tmp/dashboard-recovery-drill.json", text)

    def test_performance_probe_is_read_only(self):
        self.skipTest("perf-baseline.mjs 不随开源版分发（性能探针属部署环境工具）")
        text = PERF.read_text(encoding="utf-8")
        self.assertIn("performance.getEntriesByType", text)
        self.assertIn("MutationObserver", text)
        self.assertIn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", text)
        self.assertNotIn("writeFile", text)

    def test_foldable_smoke_has_stable_browser_fallback(self):
        text = (ROOT / "tests" / "foldable-smoke.mjs").read_text(encoding="utf-8")
        self.assertIn("BROWSER_CANDIDATES.find", text)
        self.assertIn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", text)

    def test_foldable_smoke_health_probe_respects_subpath_base(self):
        text = (ROOT / "tests" / "foldable-smoke.mjs").read_text(encoding="utf-8")
        self.assertIn("new URL('api/health?days=7', document.baseURI)", text)
        self.assertNotIn("fetch('/api/health?days=7')", text)


if __name__ == "__main__":
    unittest.main()
