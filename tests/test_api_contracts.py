"""Ensure every API route in dashboard-server.py is documented.

This is intentionally a small source-contract test: it does not start the
server or touch user data. It protects the hand-off document from drifting
when a future model adds a route.
"""
from pathlib import Path
import re
import unittest


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "dashboard-server.py"
CONTRACT = ROOT / "docs" / "api-contracts.md"


def _api_routes():
    source = SERVER.read_text(encoding="utf-8")
    routes = set()
    for method in ("GET", "POST"):
        marker = f"    def do_{method}(self):"
        start = source.index(marker)
        end = source.find("\n    def ", start + len(marker))
        if end == -1:
            end = len(source)
        section = source[start:end]
        routes.update(re.findall(r"[\"'](/api/[^\"']+)[\"']", section))
    return routes


class ApiContractDocumentationTests(unittest.TestCase):
    def test_contract_file_exists(self):
        self.assertTrue(CONTRACT.is_file())

    def test_every_source_api_route_is_documented(self):
        text = CONTRACT.read_text(encoding="utf-8")
        missing = sorted(route for route in _api_routes() if route not in text)
        self.assertEqual([], missing, "API routes missing from docs/api-contracts.md")


if __name__ == "__main__":
    unittest.main()
