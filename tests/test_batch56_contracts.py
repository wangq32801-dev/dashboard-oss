from pathlib import Path
import unittest


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = Path(__file__).resolve().parents[1]


class Batch56ContractTests(unittest.TestCase):
    def test_ai_contract_and_usage_surfaces(self):
        server = (ROOT / "dashboard-server.py").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "api-contracts.md").read_text(encoding="utf-8")
        self.assertIn('from ai_prompts import prompt_header, prompt_meta', server)
        self.assertIn('from ai_runtime import build_evidence, record_call, usage_summary', server)
        self.assertIn('"/api/ai/usage"', server)
        self.assertIn('"/api/ai/contracts"', server)
        self.assertIn('/api/ai/usage', docs)
        self.assertIn('/api/ai/contracts', docs)

    def test_view_lifecycle_is_wired_and_health_renderer_has_boundary(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        sw = (ROOT / "sw.js").read_text(encoding="utf-8")
        lifecycle = (ROOT / "frontend-modules" / "view-lifecycle.js").read_text(encoding="utf-8")
        renderer = (ROOT / "frontend-modules" / "health-render.js").read_text(encoding="utf-8")
        sse = (ROOT / "frontend-modules" / "sse-client.js").read_text(encoding="utf-8")
        self.assertIn("window.__viewLifecycleReady=import('./frontend-modules/view-lifecycle.js')", html)
        self.assertIn("window.__viewLifecycle.transition(name", html)
        self.assertIn("window.__advisorAbort&&window.__advisorAbort.abort()", html)
        self.assertIn("signal:window.__advisorAbort?.signal", html)
        self.assertIn("export function createViewLifecycle", lifecycle)
        self.assertIn("export function selectHealthRides", renderer)
        self.assertIn("__healthRender.selectHealthRides", html)
        self.assertIn("signal: externalSignal", sse)
        self.assertIn("./frontend-modules/view-lifecycle.js", sw)
        self.assertIn("./frontend-modules/health-render.js", sw)


if __name__ == "__main__":
    unittest.main()
