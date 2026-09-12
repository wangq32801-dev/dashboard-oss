from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Batch7ContractTests(unittest.TestCase):
    def test_dialog_a11y_boundary_is_cached_and_wired(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        sw = (ROOT / "sw.js").read_text(encoding="utf-8")
        module = (ROOT / "frontend-modules" / "dialog-a11y.js").read_text(encoding="utf-8")
        self.assertIn("./frontend-modules/dialog-a11y.js", html)
        self.assertIn("window.__dialogA11yReady=import('./frontend-modules/dialog-a11y.js')", html)
        self.assertIn("'./frontend-modules/dialog-a11y.js'", sw)
        self.assertIn("export function createDialogA11y", module)
        self.assertIn("body.classList.add('modal-open')", module)
        self.assertIn("aria-modal", module)
        self.assertIn("event.key === 'Escape'", module)
        self.assertIn("event.key !== 'Tab'", module)

    def test_static_dialog_labels_and_touch_budget(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        for dialog_id in ("mo-edit", "mo-role", "mo-habit", "socratic-modal", "cmdk"):
            self.assertIn(f'id="{dialog_id}"', html)
        self.assertIn('id="mo-edit" role="dialog" aria-modal="true"', html)
        self.assertIn('id="mo-role" role="dialog" aria-modal="true"', html)
        self.assertIn('id="mo-habit" role="dialog" aria-modal="true"', html)
        self.assertIn('id="cmdk" role="dialog" aria-modal="true"', html)
        self.assertIn('.modal-actions button,.modal button{min-height:44px}', html)
        self.assertIn('.modal button,.role-detail-panel button,.cmdk-item,.stat-panel button,.advisor-panel button', html)
        self.assertIn('aria-live="polite"', html)

    def test_stagger_is_idempotent(self):
        html = (ROOT / "hermes-dashboard.html").read_text(encoding="utf-8")
        self.assertIn('.reveal:not(.in):not([data-staggered])', html)
        self.assertIn("el.dataset.staggered='1'", html)


if __name__ == "__main__":
    unittest.main()
