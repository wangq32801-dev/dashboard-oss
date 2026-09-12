"""Small standard-library runtime shared by cockpit AI endpoints.

This module deliberately stores only operational metadata (feature/model/timing/
outcome).  Prompts, user messages, health values and generated text never enter
the ledger.  It is safe to use from both synchronous and streaming handlers.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone


EVIDENCE_SCHEMA = "evidence.v1"
_ledger_lock = threading.Lock()


def _ledger_path():
    root = os.environ.get("DASH_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    return os.path.expanduser(os.environ.get("DASH_AI_USAGE_FILE") or os.path.join(root, "ai_usage.json"))


def _read_ledger(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _write_ledger(path, rows):
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dash-ai-", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rows[-500:], fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        return False


def record_call(feature, model, ok, latency_ms, first_byte_ms=None,
                fallback_reason="", error_code=""):
    """Append one metadata-only call record; failure to log never breaks AI."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature": str(feature or "unknown")[:80],
        "model": str(model or "unknown")[:120],
        "ok": bool(ok),
        "latency_ms": max(0, int(latency_ms or 0)),
    }
    if first_byte_ms is not None:
        row["first_byte_ms"] = max(0, int(first_byte_ms or 0))
    if fallback_reason:
        row["fallback_reason"] = str(fallback_reason)[:160]
    if error_code:
        row["error_code"] = str(error_code)[:80]
    try:
        with _ledger_lock:
            rows = _read_ledger(_ledger_path())
            rows.append(row)
            _write_ledger(_ledger_path(), rows)
    except Exception:
        pass
    return row


def usage_summary():
    """Return bounded aggregates suitable for a private status panel."""
    with _ledger_lock:
        rows = _read_ledger(_ledger_path())[-500:]
    by_feature, by_model = {}, {}
    now = time.time()
    recent = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        feature = row.get("feature") or "unknown"
        model = row.get("model") or "unknown"
        for bucket, key in ((by_feature, feature), (by_model, model)):
            item = bucket.setdefault(key, {"requests": 0, "success": 0, "failure": 0,
                                           "fallback": 0, "avg_latency_ms": 0})
            item["requests"] += 1
            item["success"] += 1 if row.get("ok") else 0
            item["failure"] += 0 if row.get("ok") else 1
            item["fallback"] += 1 if row.get("fallback_reason") else 0
            item["avg_latency_ms"] += int(row.get("latency_ms") or 0)
        try:
            ts = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00")).timestamp()
            if now - ts <= 86400:
                recent.append(row)
        except (ValueError, TypeError, OverflowError):
            pass
    for bucket in (by_feature, by_model):
        for item in bucket.values():
            item["avg_latency_ms"] = round(item["avg_latency_ms"] / max(1, item["requests"]))
    return {
        "schema": "ai-usage.v1",
        "requests": len(rows),
        "last_24h": len(recent),
        "by_feature": by_feature,
        "by_model": by_model,
        "last": rows[-1] if rows else None,
    }


def _bounded_json(payload):
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        raw = repr(payload)
    # Hashing the snapshot is useful for reconciliation, but the values never
    # leave this function.
    return raw[:12000]


def _freshness(fetched_at, window_days):
    if not fetched_at:
        return {"status": "unknown", "age_seconds": None}
    try:
        value = str(fetched_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = max(0, int(time.time() - dt.timestamp()))
        limit = max(86400, int(window_days or 1) * 86400)
        return {"status": "stale" if age > limit else "fresh", "age_seconds": age}
    except (ValueError, TypeError, OverflowError):
        return {"status": "unknown", "age_seconds": None}


def build_evidence(scene, payload, *, window_days=None, source=None,
                   fetched_at=None, ingest=None, missing=None):
    """Create a payload-free evidence envelope for prompts and UI receipts.

    `ingest` is intentionally metadata-shaped.  When supplied by the health
    pipeline, an empty or stale receive state is made explicit instead of being
    silently interpreted as a normal zero.
    """
    scene = str(scene or "unknown").strip()[:80]
    reasons = [str(x)[:120] for x in (missing or []) if x]
    ingest = ingest if isinstance(ingest, dict) else {}
    ingest_status = str(ingest.get("status") or "").lower()
    if ingest_status in ("empty", "missing", "not_received"):
        if "hae_not_pushed" not in reasons:
            reasons.append("hae_not_pushed")
    elif ingest_status in ("stale", "old"):
        if "stale" not in reasons:
            reasons.append("stale")
    raw = _bounded_json(payload)
    observed = {
        "keys": sorted(str(k) for k in payload.keys())[:80] if isinstance(payload, dict) else [],
        "array_lengths": {str(k): len(v) for k, v in (payload.items() if isinstance(payload, dict) else [])
                          if isinstance(v, (list, tuple)) and len(v) < 100000},
    }
    return {
        "schema": EVIDENCE_SCHEMA,
        "scene": scene,
        "window_days": int(window_days) if window_days is not None else None,
        "source": str(source or ingest.get("source") or "cockpit")[:100],
        "fetched_at": str(fetched_at or ingest.get("latest_receive_at") or "")[:64],
        "freshness": _freshness(fetched_at or ingest.get("latest_receive_at"), window_days),
        "ingest": {
            "status": ingest_status or "unknown",
            "latest_receive_at": str(ingest.get("latest_receive_at") or "")[:64],
            "metric_names": sorted(str(x) for x in (ingest.get("metric_names") or [])[:80]),
        },
        "observed": observed,
        "missing_reasons": reasons[:12],
        "snapshot": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "boundary": "仅依据当前证据包；推断与建议不等于事实；缺失项不补猜",
    }


def validate_contract(meta):
    required = ("version", "input_schema", "output_schema", "evidence_boundary")
    return isinstance(meta, dict) and all(meta.get(key) for key in required)
