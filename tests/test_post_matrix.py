"""Ensure every POST endpoint has an explicit side-effect classification."""
import importlib.util
import os
import re
import unittest
from pathlib import Path


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("dashboard_server", os.path.join(ROOT, "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def _source_post_routes():
    source = Path(ROOT, "dashboard-server.py").read_text(encoding="utf-8")
    start = source.index("    def do_POST(self):")
    end = source.index("    def send_cors", start)
    return set(re.findall(r"[\"'](/api/[^\"']+)[\"']", source[start:end]))


class PostRouteMatrixTests(unittest.TestCase):
    def test_every_post_route_is_classified(self):
        routes = _source_post_routes()
        classes = dashboard.POST_ROUTE_CLASSES
        self.assertEqual(routes, set(classes), "POST route missing from explicit classification")
        self.assertEqual(set(dashboard.MUTATION_ROUTES),
                         {route for route, kind in classes.items() if kind == "mutation"})
        self.assertTrue(routes)

    def test_classification_values_are_constrained(self):
        self.assertTrue(set(dashboard.POST_ROUTE_CLASSES.values()) <= {
            "mutation", "read-only", "read-model", "diagnostic", "operator"
        })

    def test_frontend_and_backend_mutation_sets_match(self):
        html = Path(ROOT, "hermes-dashboard.html").read_text(encoding="utf-8")
        match = re.search(r"const MUTATION_ROUTES=new Set\(\[(.*?)\]\);", html, re.S)
        self.assertIsNotNone(match)
        frontend = set(re.findall(r"'([^']+)'", match.group(1)))
        backend = {route.lstrip("/") for route in dashboard.MUTATION_ROUTES}
        self.assertEqual(backend, frontend, "frontend/backend mutation route sets drifted")

    def test_bodyless_mutations_are_normalized_before_key_injection(self):
        html = Path(ROOT, "hermes-dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Object.assign({},(d&&typeof d==='object'?d:{}),{clientMutationId:mutationKey(route,d)})", html)

    def test_file_writing_refresh_mutations_return_success_for_ledger(self):
        source = Path(ROOT, "dashboard-server.py").read_text(encoding="utf-8")
        self.assertIn('dict(draft, success=True)', source)
        self.assertIn('dict(oracle, success=True)', source)


if __name__ == "__main__":
    unittest.main()
