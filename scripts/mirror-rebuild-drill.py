#!/usr/bin/env python3
"""在临时目录重建三个 Obsidian Markdown 镜像，不读取真实用户数据。"""

import importlib.util
import json
import os
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load_server():
    spec = importlib.util.spec_from_file_location(
        "dashboard_server_mirror_drill", os.path.join(ROOT, "dashboard-server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def main():
    dashboard = load_server()
    dashboard._corrupt_json_files.clear()
    with tempfile.TemporaryDirectory(prefix="dashboard-mirror-drill-") as temp:
        dashboard.DASH_RECOVERY_BACKUP_DIR = os.path.join(temp, "snapshots")
        dashboard.RELATIONS_FILE = os.path.join(temp, "relations.json")
        dashboard.LISTENING_FILE = os.path.join(temp, "listening.json")
        dashboard.PROACTIVE_FILE = os.path.join(temp, "proactive.json")
        dashboard.RELATIONS_MD = os.path.join(temp, "relations.md")
        dashboard.LISTENING_MD = os.path.join(temp, "listening.md")
        dashboard.PROACTIVE_MD = os.path.join(temp, "proactive.md")

        write_json(dashboard.RELATIONS_FILE, [
            {"id": "r1", "ts": "2026-09-01T08:00:00", "week": "2026-W36",
             "who": "测试对象", "amount": 1, "reason": "临时演练"},
            {"id": "r2", "ts": "2026-09-01T09:00:00", "deleted": True,
             "who": "不应出现", "amount": 1},
        ])
        write_json(dashboard.LISTENING_FILE, [
            {"id": "l1", "ts": "2026-09-01T08:00:00", "week": "2026-W36",
             "who": "测试对象", "feeling": "平静", "restate": "临时复述", "third": "临时选择"},
        ])
        write_json(dashboard.PROACTIVE_FILE, {
            "logs": [{"id": "p1", "ts": "2026-09-01T08:00:00", "week": "2026-W36",
                      "date": "2026-09-01", "from": "临时旧句", "to": "临时新句"}],
            "concerns": [{"id": "c1", "text": "临时关注", "circle": "influence"}],
            "checkins": {"2026-09-01": "临时选择"},
        })

        rebuilders = (
            dashboard._sync_relations_md,
            dashboard._sync_listening_md,
            dashboard._sync_proactive_md,
        )
        rebuilt = sum(1 for rebuild in rebuilders if rebuild())
        drift = dashboard.verify_mirror()
        mirror_paths = (dashboard.RELATIONS_MD, dashboard.LISTENING_MD, dashboard.PROACTIVE_MD)
        if rebuilt != 3 or drift:
            raise RuntimeError("mirror rebuild verification failed")
        for path in mirror_paths:
            if not os.path.isfile(path) or (os.stat(path).st_mode & 0o777) != 0o600:
                raise RuntimeError("mirror is missing or not owner-only")
        with open(dashboard.RELATIONS_MD, encoding="utf-8") as handle:
            if "不应出现" in handle.read():
                raise RuntimeError("soft-deleted item leaked into mirror")

        print(json.dumps({
            "success": True, "mirrors": 3, "rebuilt": rebuilt,
            "drift": len(drift), "temporary_only": True,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
