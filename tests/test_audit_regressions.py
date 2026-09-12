"""ZC-QA-1 质量审计回归锁（2026-09-04 第二版，替代 2026-09-04 第一版）。

覆盖四类契约：
1. 对外错误契约（重设计）：客户端只拿稳定 code + 固定通用文案；
   异常详情只进服务端日志；_scrub_client_text 清理 绝对路径(/etc /var /opt /usr
   /private /tmp /Library)、URL 查询参数、认证特征、控制字符、超长文本。
2. send_json 状态码契约：success:false → 400；not found → 404；成功 + mutation 上下文
   → 200 且回执落账到 *测试注入的临时路径*（绝不写 ~/.dash_mutations.json）。
3. 前端交互红线：advisor 面板与侧栏 ⌘K 提示无内联 onclick（行为级测试见
   tests/interactions.test.mjs，此处为源码锚防线）。
4. 测试卫生：本文件不写用户主目录任何文件。
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_server_audit", str(ROOT / "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class ClientErrorContractTests(unittest.TestCase):
    """客户端错误对象：稳定 code + 通用文本；敏感细节不出现。"""

    def test_internal_error_returns_generic_text_and_code(self):
        err = dashboard._client_error(FileNotFoundError(2, "No such file or directory: '/etc/passwd-extra'"))
        self.assertEqual(err["code"], "internal_error")
        self.assertEqual(err["error"], "服务器内部错误，请稍后重试")

    def test_upstream_error_code_supported(self):
        err = dashboard._client_error(RuntimeError("upstream down"), "upstream_error")
        self.assertEqual(err["code"], "upstream_error")
        self.assertEqual(err["error"], "上游服务暂时不可用，请稍后重试")

    def test_client_error_never_contains_exception_detail(self):
        cases = [
            FileNotFoundError(2, "No such file or directory: '/home/ubuntu/.dash_butler_memory.json'"),
            RuntimeError("connect ECONNREFUSED 127.0.0.1:8786 while reading /var/log/syslog"),
            ValueError("bad token=abc123 in /Users/example/secret.txt"),
        ]
        for exc in cases:
            out = dashboard._client_error(exc)
            blob = json.dumps(out, ensure_ascii=False)
            for leak in ("/home/ubuntu", "/Users/example", "/var/log", "abc123", "ECONNREFUSED"):
                self.assertNotIn(leak, blob)


class ScrubClientTextTests(unittest.TestCase):
    """纵深防御层：任何将下发的半动态文本必须经 _scrub_client_text 清洗。"""

    def test_scrubs_system_absolute_paths(self):
        for raw, frag in (
            ("读取 /etc/nginx/nginx.conf 失败", "[路径]"),
            ("无法写入 /var/lib/dash/state.json", "[路径]"),
            ("找不到 /opt/homebrew/bin/python3", "[路径]"),
            ("日志在 /usr/local/var/log/x.log", "[路径]"),
        ):
            out = dashboard._scrub_client_text(raw)
            self.assertNotIn("/etc/", out)
            self.assertNotIn("/var/", out)
            self.assertNotIn("/opt/", out)
            self.assertNotIn("/usr/", out)
            self.assertIn(frag, out)

    def test_scrubs_url_query_parameters(self):
        out = dashboard._scrub_client_text("请求 https://mcp.dida365.com/open?id=42&token=zzz 失败")
        self.assertNotIn("token=zzz", out)
        self.assertIn("?[查询参数]", out)

    def test_scrubs_auth_material(self):
        out = dashboard._scrub_client_text("Authorization: Bearer eyJhbGciOi.abc123 denied")
        self.assertNotIn("eyJhbGciOi", out)
        self.assertIn("[已脱敏]", out)
        out2 = dashboard._scrub_client_text("password=hunter2 rejected; api_key: sk-1234 invalid")
        self.assertNotIn("hunter2", out2)
        self.assertNotIn("sk-1234", out2)

    def test_scrubs_control_characters(self):
        out = dashboard._scrub_client_text("line1\x00\x1b[31m\x7fline2")
        for ch in ("\x00", "\x1b", "\x7f"):
            self.assertNotIn(ch, out)
        self.assertIn("line1", out)
        self.assertIn("line2", out)

    def test_truncates_overlong_text(self):
        self.assertEqual(len(dashboard._scrub_client_text("x" * 5000, limit=80)), 80)

    def test_home_paths_reduced(self):
        # 用运行时真实 home 构造夹具（scrubber 按当前用户 home 替换，不硬编码路径）
        home = os.path.expanduser("~")
        out = dashboard._scrub_client_text("打开 %s/Dashboard/a.json 失败" % home)
        self.assertNotIn(home, out)

    def test_exception_with_broken_str_is_still_safe(self):
        class Evil(Exception):
            def __str__(self):
                raise RuntimeError("str itself failed")

        out = dashboard._client_error(Evil())
        self.assertEqual(out["code"], "internal_error")
        self.assertNotIn("Traceback", json.dumps(out, ensure_ascii=False))


class SendJsonContractTests(unittest.TestCase):
    def _handler(self, ctx=None):
        handler = object.__new__(dashboard.DashboardHandler)
        handler._mutation_ctx = ctx
        captured = {"status": None}
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: captured.__setitem__("status", status)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_cors = lambda: None
        return handler, captured

    def _body(self, handler):
        return handler.wfile.getvalue().decode("utf-8")

    def test_success_false_is_never_http_200(self):
        handler, captured = self._handler()
        handler.send_json({"success": False, "error": "业务失败", "code": "x"})
        self.assertEqual(captured["status"], 400)

    def test_not_found_is_404(self):
        handler, captured = self._handler()
        handler.send_json({"error": "not found"})
        self.assertEqual(captured["status"], 404)

    def test_success_with_mutation_ctx_writes_receipt_to_injected_ledger(self):
        # ZC-QA-1 核心要求：回执落账路径可注入；断言真实写入；结束恢复原值。
        original = dashboard._MUTATION_LEDGER_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                ledger = os.path.join(td, "mutations.json")
                dashboard._MUTATION_LEDGER_FILE = ledger
                handler, captured = self._handler(("api/tasks/create", "qa1-key-9", {}))
                handler.send_json({"success": True, "task": {"id": "t1"}})
                self.assertEqual(captured["status"], 200)
                body = json.loads(self._body(handler))
                self.assertEqual(body["clientMutationId"], "qa1-key-9")
                self.assertIn("receipt", body)
                # 账本确实成功写入（文件存在 + 行匹配 + 可回放）
                self.assertTrue(os.path.exists(ledger), "mutation 账本必须真实落盘")
                rows = json.loads(open(ledger, encoding="utf-8").read())
                match = [r for r in rows if r.get("key") == "qa1-key-9"]
                self.assertEqual(len(match), 1)
                replay = dashboard._find_mutation_result("api/tasks/create", "qa1-key-9")
                self.assertIsNotNone(replay)
                self.assertTrue(replay["success"])
        finally:
            dashboard._MUTATION_LEDGER_FILE = original

    def test_ledger_env_override_points_away_from_home_by_default_in_tests(self):
        with unittest.mock.patch.dict(os.environ, {"DASH_MUTATION_LEDGER_FILE": "/tmp/qa1-env-check.json"}):
            spec = importlib.util.spec_from_file_location("dash_env_check", str(ROOT / "dashboard-server.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod._MUTATION_LEDGER_FILE, "/tmp/qa1-env-check.json")


class FrontendInlineHandlerContractTests(unittest.TestCase):
    def test_advisor_panel_has_no_inline_onclick(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn('onclick="advisorRegen()"', html)
        self.assertNotIn('onclick="closeAdvisor()"', html)
        self.assertNotIn("onclick=\"advisorMode(", html)
        self.assertIn('data-action="advisor-regen"', html)
        self.assertIn('data-action="advisor-close"', html)
        self.assertIn("advisor-mode", html)

    def test_cmdk_hint_is_semantic_button(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<button type="button" class="cmdk-hint" data-action="cmdk">', html)
        self.assertNotIn('onclick="openCmdk()"', html)
        self.assertIn('[data-action="cmdk"]', html)

    def test_no_raw_exception_string_reaches_client(self):
        src = (ROOT / "dashboard-server.py").read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), 1):
            if '"error": str(' in line:
                self.fail(f"未脱敏错误直出 dashboard-server.py:{lineno}")

    def test_today_focus_module_is_single_source_with_visible_fallback(self):
        # ZC-QA-1.4：今日任务选择唯一业务规则来源 = frontend-modules/today-view.js；
        # HTML 不再复制同一算法（无 fallback 双实现），模块失败显示明确降级状态。
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        self.assertIn("window.__todayViewReady", html)
        self.assertNotIn("const fallback=all.filter", html)


if __name__ == "__main__":
    unittest.main()
