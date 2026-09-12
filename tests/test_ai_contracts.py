"""Batch 5: deterministic contract tests (no network and no real user data)."""

import json
import os
import tempfile
import unittest

from ai_prompts import PROMPT_CATALOG, catalog_scenes, prompt_header, prompt_meta
from ai_runtime import build_evidence, record_call, usage_summary, validate_contract


class AIContractTests(unittest.TestCase):
    def test_every_scene_has_version_schema_and_boundary(self):
        self.assertGreaterEqual(len(catalog_scenes()), 8)
        for scene, meta in PROMPT_CATALOG.items():
            self.assertTrue(validate_contract(meta), scene)
            self.assertIn(scene, prompt_meta(scene)["scene"])
            self.assertIn("驾驶舱 AI 契约", prompt_header(scene))

    def test_evidence_is_metadata_only_and_marks_empty_ingest(self):
        raw_marker = "PRIVATE_HEALTH_VALUE_SHOULD_NOT_LEAK"
        envelope = build_evidence(
            "health", {"sleep": [{"total": raw_marker}], "notes": raw_marker},
            window_days=7,
            source="HAE",
            ingest={"status": "empty", "metric_names": ["sleep"]},
        )
        encoded = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn(raw_marker, encoded)
        self.assertIn("hae_not_pushed", envelope["missing_reasons"])
        self.assertEqual("empty", envelope["ingest"]["status"])
        self.assertEqual("evidence.v1", envelope["schema"])

    def test_evidence_accepts_frontend_freshness_shape(self):
        envelope = build_evidence(
            "advisor:health", {"freshness": {"window_days": 7, "ingest_status": "stale",
                                               "latest_receive_at": "2026-08-31T08:00:00"}},
            window_days=7,
            ingest={"status": "stale", "latest_receive_at": "2026-08-31T08:00:00"},
        )
        self.assertIn("stale", envelope["missing_reasons"])

    def test_ledger_is_bounded_and_contains_no_prompt_fields(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("DASH_AI_USAGE_FILE")
            os.environ["DASH_AI_USAGE_FILE"] = os.path.join(td, "usage.json")
            try:
                for i in range(505):
                    record_call("advisor", "model-%d" % (i % 2), i % 3 != 0, i)
                with open(os.environ["DASH_AI_USAGE_FILE"], encoding="utf-8") as fh:
                    rows = json.load(fh)
                self.assertEqual(500, len(rows))
                self.assertFalse(any("prompt" in row or "messages" in row or "content" in row for row in rows))
                summary = usage_summary()
                self.assertEqual(500, summary["requests"])
                self.assertIn("advisor", summary["by_feature"])
            finally:
                if old is None:
                    os.environ.pop("DASH_AI_USAGE_FILE", None)
                else:
                    os.environ["DASH_AI_USAGE_FILE"] = old


if __name__ == "__main__":
    unittest.main()
