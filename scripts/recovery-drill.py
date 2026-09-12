#!/usr/bin/env python3
"""无副作用的 9 域恢复演练。

所有文件和快照均在 TemporaryDirectory 中创建，脚本不会读取、覆盖或清理
当前用户的 ~/.dash_*、wiki 或 health-data。用于发布前验证恢复白名单和回读校验。
"""

import importlib.util
import json
import os
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load_server():
    spec = importlib.util.spec_from_file_location("dashboard_server_recovery_drill", os.path.join(ROOT, "dashboard-server.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    dashboard = load_server()
    dashboard._corrupt_json_files.clear()
    with tempfile.TemporaryDirectory(prefix="dashboard-recovery-drill-") as temp:
        dashboard.DASH_RECOVERY_BACKUP_DIR = os.path.join(temp, "snapshots")
        paths = {
            "local-state": os.path.join(temp, "local-state.json"),
            "relations": os.path.join(temp, "relations.json"),
            "listening": os.path.join(temp, "listening.json"),
            "health-notes": os.path.join(temp, "health-notes.json"),
            "proactive": os.path.join(temp, "proactive.json"),
            "habit-dimensions": os.path.join(temp, "habit-dimensions.json"),
            "butler-profile": os.path.join(temp, "butler-profile.json"),
            "butler-preferences": os.path.join(temp, "butler-preferences.json"),
            "advisor-outcomes": os.path.join(temp, "advisor-outcomes.json"),
        }
        dashboard.DashboardHandler.LOCAL_STATE_FILE = paths["local-state"]
        dashboard.RELATIONS_FILE = paths["relations"]
        dashboard.LISTENING_FILE = paths["listening"]
        dashboard.HEALTH_NOTES_FILE = paths["health-notes"]
        dashboard.PROACTIVE_FILE = paths["proactive"]
        dashboard.HABIT_DIMS_FILE = paths["habit-dimensions"]
        dashboard.USER_PROFILE_FILE = paths["butler-profile"]
        dashboard.BUTLER_PREFS_FILE = paths["butler-preferences"]
        dashboard.ADVISOR_OUTCOMES_FILE = paths["advisor-outcomes"]

        for index, (key, path) in enumerate(paths.items(), start=1):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"drill": key, "revision": index}, handle)
            if not dashboard._snapshot_json_before_write(path):
                raise RuntimeError("snapshot failed: " + key)
            snapshot_dir = os.path.join(dashboard.DASH_RECOVERY_BACKUP_DIR, key)
            snapshots = [name for name in os.listdir(snapshot_dir) if name.endswith(".json")]
            if not snapshots or (os.stat(os.path.join(snapshot_dir, snapshots[0])).st_mode & 0o777) != 0o600:
                raise RuntimeError("snapshot mode is not 600: " + key)

        results = {}
        for key, path in paths.items():
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"mutated": True}, handle)
            restored = dashboard._restore_json_snapshot(key)
            if (os.stat(path).st_mode & 0o777) != 0o600:
                raise RuntimeError("restored state mode is not 600: " + key)
            results[key] = bool(restored.get("success") and restored.get("verified"))

        if not all(results.values()):
            raise SystemExit("recovery drill failed: " + json.dumps(results, ensure_ascii=False))
        print(json.dumps({"success": True, "domains": len(results), "restored": sum(results.values()), "temporary_only": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
