"""Apple Watch/Health Auto Export ingestion diagnostics.

This module deliberately exposes metadata only (counts, timestamps and metric names),
never raw health values.  Keeping the scanner independent from the HTTP server makes
the ingestion contract easy to test without touching a user's real health directory.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional


def _iso(ts: float) -> str:
    """Return a stable local-time ISO timestamp for UI display."""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _date_from_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("date") or value.get("start")
    if not value:
        return ""
    text = str(value)
    return text[:10] if len(text) >= 10 else ""


def _iter_files(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return
    for day in sorted(os.listdir(root)):
        day_dir = os.path.join(root, day)
        if not os.path.isdir(day_dir):
            continue
        for name in sorted(os.listdir(day_dir)):
            if name.endswith("_ios.json"):
                yield os.path.join(day_dir, name)


def scan_ingest(root: Optional[str] = None, *, now: Optional[float] = None,
                stale_hours: int = 48) -> Dict[str, Any]:
    """Scan raw HAE files and return an ingestion-health summary.

    ``status`` is ``empty`` when no valid payload exists, ``stale`` when the newest
    valid payload is older than ``stale_hours``, and ``ok`` otherwise.  A malformed
    file is counted but never allowed to make the whole feed appear healthy. Workout
    counts distinguish records with an exported distance from records that require
    a route fallback; they never expose GPS points or health values.
    """
    data_dir = os.environ.get("DASH_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    root = os.path.expanduser(root or os.path.join(data_dir, "health-data", "raw"))
    now = float(now if now is not None else __import__("time").time())
    total = valid = invalid = metric_files = workout_files = 0
    latest_receive = latest_health = latest_workout = 0.0
    latest_data_date = ""
    metric_names = set()
    metric_last_receive = {}
    metric_latest_data_date = {}
    metric_point_counts = {}
    workout_fields = set()
    workout_latest_start = ""
    workout_count = 0
    workout_with_distance = 0
    workout_with_route = 0

    for path in _iter_files(root):
        total += 1
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("data is not an object")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            invalid += 1
            continue

        valid += 1
        try:
            received = os.path.getmtime(path)
        except OSError:
            received = 0.0
        latest_receive = max(latest_receive, received)

        metrics = data.get("metrics") or []
        workouts = data.get("workouts") or []
        if isinstance(metrics, list) and metrics:
            metric_files += 1
            latest_health = max(latest_health, received)
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                name = str(metric.get("name") or "").strip()
                if name:
                    metric_names.add(name)
                    metric_last_receive[name] = max(metric_last_receive.get(name, 0.0), received)
                points = metric.get("data") or []
                if not isinstance(points, list):
                    points = []
                if name:
                    metric_point_counts[name] = metric_point_counts.get(name, 0) + len(points)
                for point in points:
                    point_date = _date_from_value(point)
                    if point_date > latest_data_date:
                        latest_data_date = point_date
                    if name and point_date > metric_latest_data_date.get(name, ""):
                        metric_latest_data_date[name] = point_date
        if isinstance(workouts, list) and workouts:
            workout_files += 1
            latest_workout = max(latest_workout, received)
            valid_workouts = [w for w in workouts if isinstance(w, dict)]
            workout_count += len(valid_workouts)
            for workout in valid_workouts:
                workout_fields.update(str(k) for k in workout.keys() if str(k))
                distance = workout.get("distance")
                if isinstance(distance, dict):
                    distance = distance.get("qty")
                try:
                    has_distance = float(distance) > 0
                except (TypeError, ValueError):
                    has_distance = False
                if has_distance:
                    workout_with_distance += 1
                route = workout.get("route")
                if isinstance(route, dict):
                    route = (route.get("locations") or route.get("points") or
                             route.get("coordinates") or route.get("route"))
                if isinstance(route, list) and route:
                    workout_with_route += 1
                workout_date = _date_from_value(workout)
                if workout_date > latest_data_date:
                    latest_data_date = workout_date
                if workout_date > workout_latest_start:
                    workout_latest_start = workout_date

    age_hours = round(max(0.0, now - latest_receive) / 3600, 1) if latest_receive else None
    if valid == 0:
        status = "empty"
    elif age_hours is not None and age_hours > max(1, int(stale_hours)):
        status = "stale"
    else:
        status = "ok"

    return {
        "status": status,
        "root": root,
        "files": {"total": total, "valid": valid, "invalid": invalid,
                  "metric": metric_files, "workout": workout_files},
        "latest_receive_at": _iso(latest_receive) if latest_receive else "",
        "latest_health_receive_at": _iso(latest_health) if latest_health else "",
        "latest_workout_receive_at": _iso(latest_workout) if latest_workout else "",
        "latest_data_date": latest_data_date,
        # Per-metric metadata makes "HAE 没推" distinguishable from a dashboard
        # parser regression, without exposing any raw health values.
        "metric_last_receive_at": {k: _iso(v) for k, v in sorted(metric_last_receive.items()) if v},
        "metric_latest_data_date": dict(sorted(metric_latest_data_date.items())),
        "metric_point_counts": dict(sorted(metric_point_counts.items())),
        "age_hours": age_hours,
        "metric_names": sorted(metric_names),
        "workout_count": workout_count,
        "workout_with_distance": workout_with_distance,
        "workout_with_route": workout_with_route,
        "workout_missing_distance": max(0, workout_count - workout_with_distance),
        "workout_latest_start": workout_latest_start,
        "workout_fields": sorted(workout_fields),
        "stale_after_hours": int(stale_hours),
    }


__all__ = ["scan_ingest"]
