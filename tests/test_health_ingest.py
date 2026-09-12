import json
import os
import tempfile
import time
import unittest

from health_ingest import scan_ingest


class HealthIngestTests(unittest.TestCase):
    def _write(self, root, day, name, payload, mtime):
        folder = os.path.join(root, day)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.utime(path, (mtime, mtime))
        return path

    def test_reports_latest_receive_and_metric_names_without_values(self):
        with tempfile.TemporaryDirectory() as root:
            now = 1_700_000_000.0
            self._write(root, "2023-11-14", "080000_ios.json", {
                "data": {"metrics": [{"name": "heart_rate", "data": [{"date": "2023-11-14", "qty": 61}]}], "workouts": []}
            }, now - 3600)
            self._write(root, "2023-11-14", "090000_ios.json", {
                "data": {"metrics": [], "workouts": [{"id": "w1", "start": "2023-11-14T08:00:00"}]}
            }, now - 120)
            result = scan_ingest(root, now=now)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["files"]["valid"], 2)
            self.assertEqual(result["files"]["invalid"], 0)
            self.assertEqual(result["metric_names"], ["heart_rate"])
            self.assertIn("heart_rate", result["metric_last_receive_at"])
            self.assertEqual(result["metric_latest_data_date"]["heart_rate"], "2023-11-14")
            self.assertEqual(result["metric_point_counts"]["heart_rate"], 1)
            self.assertEqual(result["workout_count"], 1)
            self.assertEqual(result["workout_with_distance"], 0)
            self.assertEqual(result["workout_with_route"], 0)
            self.assertEqual(result["workout_missing_distance"], 1)
            self.assertEqual(result["workout_latest_start"], "2023-11-14")
            self.assertEqual(result["workout_fields"], ["id", "start"])
            self.assertEqual(result["latest_data_date"], "2023-11-14")
            self.assertNotIn("qty", result)

    def test_invalid_and_stale_feed_are_visible(self):
        with tempfile.TemporaryDirectory() as root:
            now = time.time()
            self._write(root, "2023-11-14", "080000_ios.json", {"broken": True}, now - 3600 * 72)
            bad = os.path.join(root, "2023-11-14", "090000_ios.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{not-json")
            os.utime(bad, (now, now))
            result = scan_ingest(root, now=now)
            self.assertEqual(result["status"], "stale")
            self.assertEqual(result["files"]["valid"], 1)
            self.assertEqual(result["files"]["invalid"], 1)

    def test_workout_coverage_stats_are_bounded_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            now = 1_700_000_000.0
            self._write(root, "2023-11-14", "100000_ios.json", {
                "data": {"metrics": [], "workouts": [
                    {"id": "reported", "start": "2023-11-14T08:00:00", "distance": {"qty": 5, "units": "km"}},
                    {"id": "route", "start": "2023-11-14T09:00:00", "route": {"locations": [{"latitude": 1, "longitude": 2}]}},
                    {"id": "missing", "start": "2023-11-14T10:00:00"},
                ]}
            }, now - 30)
            result = scan_ingest(root, now=now)
            self.assertEqual(result["files"]["workout"], 1)
            self.assertEqual(result["workout_count"], 3)
            self.assertEqual(result["workout_with_distance"], 1)
            self.assertEqual(result["workout_with_route"], 1)
            self.assertEqual(result["workout_missing_distance"], 2)
            self.assertEqual(result["workout_latest_start"], "2023-11-14")

    def test_empty_directory_has_explicit_empty_status(self):
        with tempfile.TemporaryDirectory() as root:
            result = scan_ingest(root, now=1_700_000_000.0)
            self.assertEqual(result["status"], "empty")
            self.assertIsNone(result["age_hours"])


if __name__ == "__main__":
    unittest.main()
