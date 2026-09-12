"""ZC 新视角审计回归锁（2026-09-04）。

1. 静态白名单：仓库内静态文件不再默认全部可下载——页面/manifest/SW 实测引用的
   精确文件、frontend-modules/*.js、img/ 前缀可读；历史工作产物与任意其他静态
   扩展名路径 404。非静态扩展名路径维持「兜底返回驾驶舱页」的 PWA 路由约定。
2. kb_diag.jsonl 轮转：超 KB_DIAG_MAX_BYTES 轮转 .old（保留一代），路径可注入
   临时目录——修复无界追加。
"""
import importlib.util
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_server_fresh", str(ROOT / "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class StaticAllowlistTests(unittest.TestCase):
    """真起本地 server（loopback 随机端口）验证静态白名单行为。"""

    @classmethod
    def setUpClass(cls):
        cls.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_whitelisted_assets_are_served(self):
        for path, ctype in (
            ("/hermes-dashboard.html", "text/html"),
            ("/frontend-modules/api-client.js", "javascript"),
            ("/chart.umd.min.js", "javascript"),
            ("/quotes.js", "javascript"),
            ("/sw.js", "javascript"),
            ("/manifest.webmanifest", "json"),
            ("/icon-192.png", "image/png"),
            ("/moon-crescent.png", "image/png"),
        ):
            status, got, _ = self._get(path)
            self.assertEqual(status, 200, f"{path} 应可读")
            self.assertIn(ctype, got, f"{path} Content-Type 意外：{got}")

    def test_repo_static_files_outside_allowlist_are_404(self):
        for path in (
            "/_sw_work2.js",
            "/verify.js",
            "/guide.js",
            "/_vps_review.png",
            "/benchmark-research.html",
            "/life-tree.html",
        ):
            status, _, _ = self._get(path)
            self.assertEqual(status, 404, f"{path} 是历史工作产物，不应可下载")

    def test_source_and_docs_still_fallback_to_shell_page(self):
        # .py/.sh/.md 不在 STATIC_EXTS：维持「兜底返回驾驶舱页」约定（PWA 路由依赖），
        # 返回的是 HTML 壳而非文件内容。
        for path in ("/dashboard-server.py", "/scripts/sync-dashboard.sh", "/docs/quality-audit-report.md"):
            status, ctype, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("text/html", ctype, path)
            self.assertIn(b"view-today", body, path)  # 是壳页，不是源文件

    def test_dotdot_segments_cannot_escape_allowlist(self):
        # path-as-is：http.client 不规范化请求行，.. 段原样到达服务端；
        # 规范化后落在白名单外 → 必须 404（修复前 /frontend-modules/../verify.js 可下载真文件）。
        import http.client
        host, port = "127.0.0.1", self.server.server_port
        for target in (
            "/frontend-modules/../verify.js",
            "/frontend-modules/..%2fverify.js",
            "/img/../verify.js",
        ):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.putrequest("GET", target, skip_host=False)
                conn.endheaders()
                resp = conn.getresponse()
                status = resp.status
                resp.read()
            finally:
                conn.close()
            self.assertEqual(status, 404, f"{target} 穿越必须被拒")

    def test_dotdot_to_nonstatic_ext_keeps_shell_fallback(self):
        # 非静态扩展名（.py/.md 等）维持既有「兜底返回驾驶舱页」约定：200 但必须是
        # HTML 壳，绝不能是目标文件内容（源码零泄漏）。
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.putrequest("GET", "/frontend-modules/../../etc/passwd")
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"<!DOCTYPE html>", body[:200])
        self.assertNotIn(b"root:", body)

    def test_dotdot_source_file_served_shell_fallback(self):
        # .py 非静态扩展名：即使带 .. 段也走兜底（HTML 壳），绝不能是源码内容
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.putrequest("GET", "/frontend-modules/../../dashboard-server.py")
            conn.endheaders()
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"<!DOCTYPE html>", body[:200])


class KbDiagRotationTests(unittest.TestCase):
    def test_rotation_via_injectable_path(self):
        with tempfile.TemporaryDirectory() as td:
            original_path, original_max = dashboard.KB_DIAG_FILE, dashboard.KB_DIAG_MAX_BYTES
            kb = os.path.join(td, "kb_diag.jsonl")
            try:
                dashboard.KB_DIAG_FILE = kb
                dashboard.KB_DIAG_MAX_BYTES = 5
                dashboard._kb_diag_append({"k": 1})
                self.assertTrue(os.path.exists(kb))
                dashboard._kb_diag_append({"k": 2})  # 触发轮转（单条 9B > 5B 阈值）
                self.assertTrue(os.path.exists(kb + ".old"), "超限后必须轮转出 .old")
                with open(kb, encoding="utf-8") as f:
                    rows = [json.loads(x) for x in f if x.strip()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["k"], 2)
                with open(kb + ".old", encoding="utf-8") as f:
                    old_rows = [json.loads(x) for x in f if x.strip()]
                self.assertEqual(old_rows[0]["k"], 1)
            finally:
                dashboard.KB_DIAG_FILE, dashboard.KB_DIAG_MAX_BYTES = original_path, original_max

    def test_under_limit_never_rotates(self):
        with tempfile.TemporaryDirectory() as td:
            original_path, original_max = dashboard.KB_DIAG_FILE, dashboard.KB_DIAG_MAX_BYTES
            kb = os.path.join(td, "kb_diag.jsonl")
            try:
                dashboard.KB_DIAG_FILE = kb
                dashboard.KB_DIAG_MAX_BYTES = 1024 * 1024
                for i in range(5):
                    dashboard._kb_diag_append({"i": i})
                self.assertFalse(os.path.exists(kb + ".old"))
                self.assertEqual(sum(1 for _ in open(kb, encoding="utf-8")), 5)
            finally:
                dashboard.KB_DIAG_FILE, dashboard.KB_DIAG_MAX_BYTES = original_path, original_max


if __name__ == "__main__":
    unittest.main()
