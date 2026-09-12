"""IA-2.1 · _health_alerts 睡眠效率告警回归（维护手册 118 章缺陷的防回归锁）。

背景：118 章修复前，「近7晚睡眠效率 %.0f%%（<85%）」文案里未转义的 `（<85%）`
让 %-formatting 抛 TypeError，被 _health_alerts 外层 try/except 吞掉 → 整个
预警列表静默变空（其他告警也一起丢失）。本测试锁住四个不变量：
1. sleep_eff < 85 时函数不抛异常且返回 eff 告警；
2. 文案含正确的百分号渲染（如 80%（<85%））；
3. eff 告警不挤掉/不被挤掉其他规则告警（构造 debt 同触，双告警并存）；
4. eff ≥ 85 或缺失时不产出 eff 告警。
纯函数级测试：object.__new__ 构造 Handler，不启动服务、不触碰任何数据文件。
"""
import importlib.util
import os
import unittest


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("dashboard_server_health_alerts", os.path.join(ROOT, "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def _payload(sleep_eff=None, sleep_debt=None):
    derived = {}
    if sleep_eff is not None:
        derived["sleep_eff"] = sleep_eff
    if sleep_debt is not None:
        derived["sleep_debt"] = sleep_debt
    return {"derived": derived, "hrv": [], "rhr": [], "quality": {}}


class HealthAlertsSleepEffTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(dashboard.DashboardHandler)

    def _keys(self, alerts):
        return [a.get("key") for a in alerts]

    def test_low_sleep_eff_returns_eff_alert_without_exception(self):
        alerts = self.handler._health_alerts(_payload(sleep_eff=80))
        self.assertIn("eff", self._keys(alerts))

    def test_eff_text_renders_percent_correctly(self):
        alerts = self.handler._health_alerts(_payload(sleep_eff=80))
        eff = next(a for a in alerts if a.get("key") == "eff")
        self.assertIn("80%（<85%）", eff["text"])

    def test_eff_alert_does_not_wipe_other_alerts(self):
        # 修复前：eff 文案格式错误 → 异常被吞 → 整个列表为空（debt 一起丢）。
        alerts = self.handler._health_alerts(_payload(sleep_eff=80, sleep_debt=6.0))
        keys = self._keys(alerts)
        self.assertIn("eff", keys)
        self.assertIn("debt", keys)
        self.assertGreaterEqual(len(alerts), 2)

    def test_eff_at_or_above_85_not_alerted(self):
        self.assertNotIn("eff", self._keys(self.handler._health_alerts(_payload(sleep_eff=85))))
        self.assertNotIn("eff", self._keys(self.handler._health_alerts(_payload(sleep_eff=91.4))))

    def test_missing_eff_not_alerted(self):
        self.assertNotIn("eff", self._keys(self.handler._health_alerts(_payload())))

    def test_eff_fractional_value_formatting(self):
        alerts = self.handler._health_alerts(_payload(sleep_eff=78.45))
        eff = next(a for a in alerts if a.get("key") == "eff")
        self.assertIn("78%（<85%）", eff["text"])  # %.0f 四舍五入到 78

    def test_numeric_strings_are_accepted_without_losing_other_alerts(self):
        alerts = self.handler._health_alerts(_payload(sleep_eff="80", sleep_debt="6.0"))
        self.assertIn("eff", self._keys(alerts))
        self.assertIn("debt", self._keys(alerts))

    def test_malformed_eff_does_not_wipe_valid_debt_alert(self):
        alerts = self.handler._health_alerts(_payload(sleep_eff="oops", sleep_debt=6.0))
        self.assertNotIn("eff", self._keys(alerts))
        self.assertIn("debt", self._keys(alerts))

    def test_malformed_series_items_do_not_wipe_valid_debt_alert(self):
        payload = _payload(sleep_debt=6.0)
        payload.update({"hrv": [None, {"qty": "bad"}], "rhr": ["bad"], "quality": {"invalid": {"hr": "2"}}})
        alerts = self.handler._health_alerts(payload)
        self.assertIn("debt", self._keys(alerts))

    def test_non_array_series_do_not_wipe_valid_debt_alert(self):
        payload = _payload(sleep_debt=6.0)
        payload.update({"hrv": 42, "resp": {"qty": 15}})
        self.assertIn("debt", self._keys(self.handler._health_alerts(payload)))


if __name__ == "__main__":
    unittest.main()
