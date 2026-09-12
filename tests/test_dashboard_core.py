import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock


import os as _os
_os.environ["DASH_DEMO"] = "0"  # 测试固定真实模式（mock TickTick 链路，不进演示分支）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("dashboard_server", os.path.join(ROOT, "dashboard-server.py"))
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


class DashboardCoreTests(unittest.TestCase):
    def setUp(self):
        dashboard._corrupt_json_files.clear()

    def test_corrupt_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")
            self.assertEqual(dashboard._read_json(path, []), [])
            self.assertFalse(dashboard._write_json(path, [{"new": True}]))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{broken")

    def test_atomic_text_write_replaces_file_without_partial_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "role.md")
            self.assertTrue(dashboard._write_text_atomic(path, "旧内容"))
            self.assertTrue(dashboard._write_text_atomic(path, "新内容\n完整"))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "新内容\n完整")
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_user_profile_corruption_is_backed_up_and_self_healed(self):
        original_path = dashboard.USER_PROFILE_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.USER_PROFILE_FILE = os.path.join(td, "profile.json")
                with open(dashboard.USER_PROFILE_FILE, "w", encoding="utf-8") as f:
                    f.write("{broken")
                profile = dashboard._load_user_profile()
                self.assertEqual(profile["identity"], dashboard.USER_PROFILE_SEED["identity"])
                self.assertTrue(os.path.exists(dashboard.USER_PROFILE_FILE))
                self.assertTrue(any(name.startswith("profile.json.corrupt-") for name in os.listdir(td)))
                with open(dashboard.USER_PROFILE_FILE, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["identity"], dashboard.USER_PROFILE_SEED["identity"])
        finally:
            dashboard.USER_PROFILE_FILE = original_path
            dashboard._corrupt_json_files.clear()

    def test_local_state_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "local.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_revision": 3, "completed": []}, f)
            handler = object.__new__(dashboard.DashboardHandler)
            handler.LOCAL_STATE_FILE = path
            result = handler._save_local_state({"_revision": 2, "completed": []})
            self.assertTrue(result["conflict"])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["_revision"], 3)

    def test_local_state_advances_revision_and_allows_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "local.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_revision": 3, "trash": [{"id": "old"}]}, f)
            handler = object.__new__(dashboard.DashboardHandler)
            handler.LOCAL_STATE_FILE = path
            result = handler._save_local_state({"_revision": 3, "trash": []})
            self.assertTrue(result["success"])
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["_revision"], 4)
            self.assertEqual(saved["trash"], [])

    def test_local_state_creates_and_prunes_write_ahead_backups(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "local.json")
            backup_dir = os.path.join(td, "backups")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_revision": 1, "mission": "原状态"}, f)
            handler = object.__new__(dashboard.DashboardHandler)
            handler.LOCAL_STATE_FILE = path
            handler.LOCAL_STATE_BACKUP_DIR = backup_dir
            handler.LOCAL_STATE_BACKUP_LIMIT = 2
            for revision in (1, 2, 3):
                result = handler._save_local_state({"_revision": revision, "mission": f"版本{revision + 1}"})
                self.assertTrue(result["success"])
                self.assertTrue(result["backup"])
            backups = sorted(os.listdir(backup_dir))
            self.assertEqual(len(backups), 2)
            with open(os.path.join(backup_dir, backups[-1]), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["mission"], "版本3")

    def test_corrupt_local_state_cannot_be_overwritten_by_sync(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "local.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")
            handler = object.__new__(dashboard.DashboardHandler)
            handler.LOCAL_STATE_FILE = path
            result = handler._save_local_state({"_revision": 0, "completed": []})
            self.assertFalse(result["success"])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{broken")

    def test_local_state_rejects_malformed_revision_and_does_not_persist_transport_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "local.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_revision": 1, "mission": "旧"}, f)
            handler = object.__new__(dashboard.DashboardHandler)
            handler.LOCAL_STATE_FILE = path
            malformed = handler._save_local_state({"_revision": "nope", "mission": "新"})
            self.assertFalse(malformed["success"])
            saved = handler._save_local_state({"_revision": 1, "clientMutationId": "m1", "mission": "新"})
            self.assertTrue(saved["success"])
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            self.assertNotIn("clientMutationId", state)
            self.assertEqual(state["mission"], "新")

    def test_archive_uses_tag_and_verifies_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        before = {"id": "t1", "pid": "p1", "tags": ["x"], "title": "task"}
        after = {**before, "tags": ["x", "archived"]}
        with mock.patch.object(dashboard, "_fetch_project_tasks", side_effect=[[before], [after]]), \
             mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.set_task_archived({"id": "t1", "projectId": "p1"}, True)
        self.assertTrue(result["success"])
        self.assertTrue(result["archived"])

    def test_archive_can_verify_completed_task_via_completed_listing(self):
        handler = object.__new__(dashboard.DashboardHandler)
        before = {"id": "done1", "pid": "p1", "tags": ["x"], "title": "finished"}
        after = {**before, "tags": ["x", "archived"]}
        with mock.patch.object(dashboard, "_fetch_project_tasks", return_value=[]), \
             mock.patch.object(dashboard, "_fetch_completed_project_tasks", side_effect=[[before], [after]]), \
             mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.set_task_archived({"id": "done1", "projectId": "p1"}, True)
        self.assertTrue(result["success"])
        self.assertTrue(result["archived"])

    def test_sync_role_persists_emoji_in_frontmatter(self):
        original_dir = dashboard.OBSIDIAN_ROLES_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.OBSIDIAN_ROLES_DIR = td
                path = os.path.join(td, "学习者.md")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("---\nrole: 学习者\nemoji: 🧠\n---\n\n## 使命宣言\n持续学习\n")
                handler = object.__new__(dashboard.DashboardHandler)
                result = handler.sync_role({"name": "学习者", "emoji": "🚀"})
                self.assertTrue(result["success"])
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("emoji: 🚀", text)
                self.assertEqual(text.count("emoji:"), 1)
        finally:
            dashboard.OBSIDIAN_ROLES_DIR = original_dir

    def test_sync_role_rename_refuses_overwriting_existing_role(self):
        original_dir = dashboard.OBSIDIAN_ROLES_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.OBSIDIAN_ROLES_DIR = td
                with open(os.path.join(td, "旧角色.md"), "w", encoding="utf-8") as f:
                    f.write("---\nrole: 旧角色\n---\n")
                with open(os.path.join(td, "已有角色.md"), "w", encoding="utf-8") as f:
                    f.write("---\nrole: 已有角色\n---\n保留\n")
                handler = object.__new__(dashboard.DashboardHandler)
                result = handler.sync_role({"old_name": "旧角色", "name": "旧角色", "new_name": "已有角色"})
                self.assertFalse(result["success"])
                self.assertIn("未覆盖", result["error"])
                self.assertTrue(os.path.exists(os.path.join(td, "旧角色.md")))
                with open(os.path.join(td, "已有角色.md"), encoding="utf-8") as f:
                    self.assertIn("保留", f.read())
        finally:
            dashboard.OBSIDIAN_ROLES_DIR = original_dir

    def test_create_role_requires_parseable_readback(self):
        original_dir = dashboard.OBSIDIAN_ROLES_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.OBSIDIAN_ROLES_DIR = td
                handler = object.__new__(dashboard.DashboardHandler)
                result = handler.create_role({"name": "测试角色", "emoji": "🧪", "mission": "验证"})
                self.assertTrue(result["success"])
                self.assertEqual(result["role"]["id"], "测试角色")
        finally:
            dashboard.OBSIDIAN_ROLES_DIR = original_dir

    def test_delete_role_requires_move_readback(self):
        original_dir = dashboard.OBSIDIAN_ROLES_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.OBSIDIAN_ROLES_DIR = td
                path = os.path.join(td, "测试角色.md")
                with open(path, "w", encoding="utf-8") as f:
                    f.write("---\nrole: 测试角色\n---\n")
                handler = object.__new__(dashboard.DashboardHandler)
                result = handler.delete_role({"name": "测试角色"})
                self.assertTrue(result["success"])
                self.assertFalse(os.path.exists(path))
        finally:
            dashboard.OBSIDIAN_ROLES_DIR = original_dir

    def test_due_clear_requires_readback_confirmation(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}) as mcp, \
             mock.patch.object(dashboard, "_fetch_project_tasks", return_value=[{"id":"t1","dueDate":"2026-08-30T09:00:00+08:00"}]):
            result = handler.update_task({"id": "t1", "projectId": "p1", "dueDate": None})
        self.assertFalse(result["success"])
        self.assertIn("未确认日期已清空", result["error"])
        mcp.assert_called_once()

    def test_update_task_accepts_only_completion_status_values(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}) as mcp, \
             mock.patch.object(dashboard, "_fetch_project_tasks", return_value=[{"id": "t1", "title": "", "content": "", "tags": []}]):
            result = handler.update_task({"id": "t1", "projectId": "p1", "status": 0})
        self.assertTrue(result["success"])
        payload = mcp.call_args.args[1]["task"]
        self.assertEqual(payload["status"], 0)
        with mock.patch.object(dashboard, "call_ticktick_mcp") as mcp:
            bad = handler.update_task({"id": "t1", "projectId": "p1", "status": 1})
        self.assertFalse(bad["success"])
        self.assertIn("status 必须是 0", bad["error"])
        mcp.assert_not_called()

    def test_update_task_rejects_success_without_matching_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}), \
             mock.patch.object(dashboard, "_fetch_project_tasks", return_value=[]), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.update_task({"id": "t1", "projectId": "p1", "title": "new title"})
        self.assertFalse(result["success"])
        self.assertIn("回读未验证", result["error"])

    def test_complete_task_requires_completed_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        completed = {"id": "t1", "status": 2, "title": "done"}
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}) as mcp, \
             mock.patch.object(dashboard, "_fetch_completed_project_tasks", side_effect=[[], [completed]]), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.complete_task({"id": "t1", "projectId": "p1"})
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["id"], "t1")
        self.assertEqual(mcp.call_args.args[0], "update_task")
        self.assertEqual(mcp.call_args.args[1]["task"]["status"], 2)

    def test_complete_task_rejects_unverified_success(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}), \
             mock.patch.object(dashboard, "_fetch_completed_project_tasks", return_value=[]), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.complete_task({"id": "t1", "projectId": "p1"})
        self.assertFalse(result["success"])
        self.assertIn("回读未验证到完成状态", result["error"])

    def test_proactive_write_reports_disk_failure(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(handler, "_raw_proactive", return_value={"concerns": [], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=False), \
             mock.patch.object(dashboard, "_sync_mirror"):
            result = handler.post_proactive({"concerns": [{"id": "c1", "text": "x", "circle": "concern"}]})
        self.assertFalse(result["success"])
        self.assertIn("数据写入失败", result["error"])

    def test_relation_and_listening_writes_surface_mirror_warning(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_read_json", side_effect=lambda p, d: []), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            rel = handler.post_relation({"entry": {"who": "小明", "reason": "问候"}})
            note = handler.post_listening({"note": {"who": "小明", "feeling": "平静"}})
        self.assertTrue(rel["success"]); self.assertIn("warning", rel)
        self.assertTrue(note["success"]); self.assertIn("warning", note)

    def test_relation_and_listening_reject_malformed_payloads_before_write(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "_write_json") as write:
            rel = handler.post_relation({"entry": "not-an-object"})
            rel_empty = handler.post_relation({"entry": {"who": "  ", "reason": ""}})
            note = handler.post_listening({"note": ["not-an-object"]})
            note_empty = handler.post_listening({"note": {"who": "小明"}})
        self.assertFalse(rel["success"])
        self.assertFalse(rel_empty["success"])
        self.assertFalse(note["success"])
        self.assertFalse(note_empty["success"])
        write.assert_not_called()

    def test_soft_delete_rejects_missing_relation_and_listening_ids(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "_read_json", return_value=[]), \
             mock.patch.object(dashboard, "_write_json") as write, \
             mock.patch.object(dashboard, "_undo_push") as undo:
            rel = handler.delete_relation({"id": "missing-rel"})
            note = handler.delete_listening({"id": "missing-note"})
        self.assertFalse(rel["success"])
        self.assertFalse(note["success"])
        write.assert_not_called()
        undo.assert_not_called()

    def test_week_plan_and_proactive_reject_invalid_shapes_before_mcp_or_write(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp") as mcp, \
             mock.patch.object(handler, "_raw_proactive", return_value={"concerns": [], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json") as write:
            rock = handler.post_week_plan({"title": "  \n", "project": "📥 收集箱"})
            concerns = handler.post_proactive({"concerns": ["bad"]})
            log = handler.post_proactive({"log": "bad"})
        self.assertFalse(rock["success"])
        self.assertFalse(concerns["success"])
        self.assertFalse(log["success"])
        mcp.assert_not_called()
        write.assert_not_called()

    def test_update_endpoints_reject_empty_or_invalid_changes_before_write(self):
        handler = object.__new__(dashboard.DashboardHandler)
        rel = {"id": "r1", "who": "小明", "reason": "问候", "type": "存款", "amount": 10}
        note = {"id": "l1", "who": "小明", "feeling": "平静"}
        pro = {"concerns": [], "logs": [{"id": "p1", "from": "旧", "to": "新"}], "checkins": {}}
        def read_json(path, default):
            if path == dashboard.RELATIONS_FILE:
                return [dict(rel)]
            if path == dashboard.LISTENING_FILE:
                return [dict(note)]
            return default
        with mock.patch.object(dashboard, "_read_json", side_effect=read_json), \
             mock.patch.object(handler, "_raw_proactive", return_value=pro), \
             mock.patch.object(dashboard, "_write_json") as write, \
             mock.patch.object(dashboard, "call_ticktick_mcp") as mcp:
            rel_empty = handler.update_relation({"id": "r1"})
            rel_bad_amount = handler.update_relation({"id": "r1", "amount": "bad"})
            note_empty = handler.update_listening({"id": "l1", "note": {}})
            pro_empty = handler.update_proactive({"id": "p1"})
            habit_zero = handler.update_habit({"id": "h1", "goal": 0})
        self.assertFalse(rel_empty["success"])
        self.assertFalse(rel_bad_amount["success"])
        self.assertFalse(note_empty["success"])
        self.assertFalse(pro_empty["success"])
        self.assertFalse(habit_zero["success"])
        write.assert_not_called()
        mcp.assert_not_called()

    def test_health_note_requires_text_payload(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "_write_json") as write:
            result = handler.post_health_note({"note": ["not", "text"]})
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "note 必须是文本")
        write.assert_not_called()

    def test_mirror_response_preserves_success_and_adds_warning_only_on_failure(self):
        payload = {"success": True, "entry": {"id": "r1"}}
        self.assertIs(dashboard._mirror_response(payload, True), payload)
        self.assertNotIn("warning", payload)
        dashboard._mirror_response(payload, False)
        self.assertIn("warning", payload)

    def test_proactive_concern_write_syncs_mirror_and_surfaces_warning(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(handler, "_raw_proactive", return_value={"concerns": [], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False) as mirror:
            result = handler.post_proactive({"concerns": [{"id": "c1", "text": "x", "circle": "concern"}]})
        self.assertTrue(result["success"])
        self.assertIn("warning", result)
        mirror.assert_called_once()

    def test_proactive_concern_write_preserves_soft_deleted_entries(self):
        handler = object.__new__(dashboard.DashboardHandler)
        deleted = {"id": "old", "text": "可恢复", "circle": "concern", "deleted": True,
                   "deleted_at": "2026-08-30"}
        active = {"id": "new", "text": "当前", "circle": "influence"}
        captured = {}
        def capture(path, payload):
            captured["payload"] = payload
            return True
        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [deleted], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", side_effect=capture), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=True):
            result = handler.post_proactive({"concerns": [active]})
        self.assertTrue(result["success"])
        saved = captured["payload"]["concerns"]
        self.assertEqual([x["id"] for x in saved], ["old", "new"])
        self.assertEqual(result["concerns"], 1)
        self.assertEqual(result["influence"], 1)

    def test_proactive_concern_write_preserves_concurrent_active_entries(self):
        handler = object.__new__(dashboard.DashboardHandler)
        current = {"id": "remote", "text": "另一端新增", "circle": "concern"}
        incoming = {"id": "local", "text": "本端修改", "circle": "influence"}
        captured = {}
        def capture(path, payload):
            captured["payload"] = payload
            return True
        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [current], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", side_effect=capture), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=True):
            result = handler.post_proactive({"concerns": [incoming]})
        self.assertTrue(result["success"])
        self.assertEqual({x["id"] for x in captured["payload"]["concerns"]}, {"local", "remote"})
        self.assertEqual(result["concerns"], 2)

    def test_proactive_delete_restore_and_update_surface_mirror_warning(self):
        handler = object.__new__(dashboard.DashboardHandler)
        concern = {"id": "c1", "text": "x", "circle": "concern"}
        log = {"id": "l1", "from": "旧", "to": "新"}
        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [concern], "logs": [log], "checkins": {"2026-08-29": "今天"}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            deleted = handler.delete_proactive({"id": "c1", "kind": "concern"})
            self.assertTrue(deleted["success"])
            self.assertIn("warning", deleted)

        restored = {"id": "c1", "text": "x", "circle": "concern", "deleted": True,
                    "deleted_at": "2026-08-29"}
        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [restored], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            restored_result = handler.restore_proactive({"id": "c1", "kind": "concern"})
            self.assertTrue(restored_result["success"])
            self.assertIn("warning", restored_result)

        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [], "logs": [{"id": "l1", "from": "旧", "to": "新"}], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            updated = handler.update_proactive({"id": "l1", "from": "改后", "to": "新"})
            self.assertTrue(updated["success"])
            self.assertIn("warning", updated)

    def test_butler_proactive_actions_surface_mirror_warning(self):
        handler = object.__new__(dashboard.DashboardHandler)
        concern = {"id": "c1", "text": "焦虑", "circle": "concern"}
        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [{"id": "c1", "text": "焦虑", "circle": "concern"}], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            deleted = handler._butler_act_core("删关注", {"关键词": "焦虑"})
        self.assertTrue(deleted["success"])
        self.assertIn("warning", deleted)

        with mock.patch.object(handler, "_raw_proactive", return_value={
                "concerns": [concern], "logs": [], "checkins": {}}), \
             mock.patch.object(dashboard, "_write_json", return_value=True), \
             mock.patch.object(dashboard, "_sync_mirror", return_value=False):
            updated = handler._butler_act_core("改关注", {"关键词": "焦虑", "新内容": "行动"})
        self.assertTrue(updated["success"])
        self.assertIn("warning", updated)

    def test_coach_proactive_gap_only_appears_when_no_influence_items(self):
        handler = object.__new__(dashboard.DashboardHandler)
        base = {"roles": [], "tasks": [], "habits": [], "chain_health": {}}
        with mock.patch.object(dashboard, "cached", return_value=base), \
             mock.patch.object(handler, "_get_health", return_value={}), \
             mock.patch.object(handler, "get_week_plan", return_value={"big_rocks": []}), \
             mock.patch.object(handler, "_raw_proactive", return_value={
                 "concerns": [{"id": "c1", "text": "可控行动", "circle": "influence"}],
                 "logs": [], "checkins": {}}):
            cards = handler.coach_cards()["cards"]
        self.assertFalse(any(c["view"] == "proactive" for c in cards))

        with mock.patch.object(dashboard, "cached", return_value=base), \
             mock.patch.object(handler, "_get_health", return_value={}), \
             mock.patch.object(handler, "get_week_plan", return_value={"big_rocks": []}), \
             mock.patch.object(handler, "_raw_proactive", return_value={
                 "concerns": [{"id": "c1", "text": "外部焦虑", "circle": "concern"}],
                 "logs": [], "checkins": {}}):
            cards = handler.coach_cards()["cards"]
        self.assertTrue(any(c["view"] == "proactive" for c in cards))

    def test_coach_role_gaps_focus_the_specific_role_card(self):
        handler = object.__new__(dashboard.DashboardHandler)
        base = {"roles": [{"id": "r1", "name": "学习者", "key_results": [], "mission": ""}],
                "tasks": [], "habits": [], "chain_health": {}}
        with mock.patch.object(dashboard, "cached", return_value=base), \
             mock.patch.object(handler, "_get_health", return_value={}), \
             mock.patch.object(handler, "get_week_plan", return_value={"big_rocks": []}), \
             mock.patch.object(handler, "_raw_proactive", return_value={"concerns": [], "logs": [], "checkins": {}}):
            cards = handler.coach_cards()["cards"]
        role_card = next(c for c in cards if c["view"] == "roles")
        self.assertEqual(role_card["cta"]["focus"], "#role-r1")

    def test_health_note_write_reports_disk_failure(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dashboard, "HEALTH_NOTES_FILE", os.path.join(td, "health-notes.json")), \
             mock.patch.object(dashboard, "_write_json", return_value=False):
            result = handler.post_health_note({"note": "睡得不错"})
        self.assertFalse(result["success"])
        self.assertIn("数据写入失败", result["error"])

    def test_create_task_rejects_success_without_task_id(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}):
            result = handler.create_task({"title": "测试任务", "project": "📥 收集箱"})
        self.assertFalse(result["success"])
        self.assertIn("未提供新任务 ID", result["error"])

    def test_create_task_rejects_blank_title_before_mcp(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp") as mcp:
            result = handler.create_task({"title": "  \n\t", "project": "📥 收集箱"})
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "缺少标题")
        mcp.assert_not_called()

    def test_create_habit_rejects_invalid_numeric_target_before_mcp(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp") as mcp:
            result = handler.create_habit({"name": "  晨读  ", "goal": "not-a-number"})
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "目标值和目标天数必须是数字")
        mcp.assert_not_called()

    def test_create_task_requires_authoritative_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with (mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[
                {"result": {"structuredContent": {"id": "new-1"}}},
                {"result": {"structuredContent": {"tasks": [{"id": "new-1", "projectId": dashboard.PROJECT_IDS["📥 收集箱"], "title": "测试任务"}]}}}]),
             mock.patch.object(dashboard.time, "sleep", return_value=None)):
            result = handler.create_task({"title": "测试任务", "project": "📥 收集箱"})
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["id"], "new-1")

    def test_delete_task_requires_authoritative_absence_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with (mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[
                {"result": {}},
                {"result": {"structuredContent": {"tasks": []}}},
                {"result": {"structuredContent": {"result": []}}}]),
             mock.patch.object(dashboard.time, "sleep", return_value=None)):
            result = handler.delete_task({"id": "gone-1", "projectId": dashboard.PROJECT_IDS["📥 收集箱"]})
        self.assertTrue(result["success"])

    def test_delete_task_rejects_unverified_success(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with (mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[
                {"result": {}},
                {"result": {"structuredContent": {"tasks": [{"id": "still-here"}]}}},
                {"result": {"structuredContent": {"result": []}}},
                {"result": {"structuredContent": {"tasks": [{"id": "still-here"}]}}},
                {"result": {"structuredContent": {"result": []}}}]),
             mock.patch.object(dashboard.time, "sleep", return_value=None)):
            result = handler.delete_task({"id": "still-here", "projectId": dashboard.PROJECT_IDS["📥 收集箱"]})
        self.assertFalse(result["success"])
        self.assertIn("回读仍找到", result["error"])

    def test_week_plan_rejects_success_without_task_id(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp", return_value={"result": {}}):
            result = handler.post_week_plan({"title": "本周大石头", "project": "📥 收集箱"})
        self.assertFalse(result["success"])
        self.assertIn("未提供大石头任务 ID", result["error"])

    def test_week_plan_rejects_unknown_project_before_remote_write(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "call_ticktick_mcp") as mcp:
            result = handler.post_week_plan({"title": "本周大石头", "project": "不存在的项目"})
        self.assertFalse(result["success"])
        self.assertIn("未知项目", result["error"])
        mcp.assert_not_called()

    def test_habit_dimension_reports_disk_failure(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dashboard, "HABIT_DIMS_FILE", os.path.join(td, "dims.json")), \
             mock.patch.object(dashboard, "_write_json", return_value=False):
            result = handler.set_habit_dimension({"name": "阅读", "role": "智者"})
        self.assertFalse(result["success"])
        self.assertIn("习惯维度写入失败", result["error"])

    def test_habit_update_requires_readback_and_migrates_dimension(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dashboard, "HABIT_DIMS_FILE", os.path.join(td, "dims.json")), \
             mock.patch.object(dashboard, "_write_json", side_effect=lambda p, d: (open(p, "w", encoding="utf-8").write(json.dumps(d)) or True)), \
             mock.patch.object(dashboard, "_load_habit_dims", return_value={"旧习惯": {"dimension": "智力"}}), \
             mock.patch.object(dashboard, "_save_habit_dims", return_value=True) as save_dims, \
             mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[{"result": {}}, {"result": {"structuredContent": {"result": [{"id": "h1", "name": "新习惯", "goal": 2}]}}}]), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.update_habit({"id": "h1", "name": "新习惯", "goal": 2, "oldName": "旧习惯"})
        self.assertTrue(result["success"])
        self.assertEqual(result["habit"]["name"], "新习惯")
        save_dims.assert_called_once_with({"新习惯": {"dimension": "智力"}})

    def test_habit_checkin_requires_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        today = int(dashboard.time.strftime("%Y%m%d"))
        with mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[
                {"result": {}},
                {"result": {"structuredContent": {"result": [{"habitId": "h1", "checkins": [{"stamp": today, "value": 1}]}]}}}],
                ), mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.checkin_habit({"habitId": "h1", "value": 1})
        self.assertTrue(result["success"])
        self.assertEqual(result["value"], 1)

    def test_butler_preference_and_rule_writes_report_disk_failure(self):
        with mock.patch.object(dashboard, "_write_json", return_value=False), \
             mock.patch.object(dashboard, "_load_butler_rules", return_value=[]), \
             mock.patch.object(dashboard, "_load_butler_prefs", return_value={}):
            self.assertIsNone(dashboard._save_butler_rule("不要假成功"))
            self.assertIsNone(dashboard._save_butler_pref("tone", "简洁"))

    def test_butler_history_clear_reports_disk_failure(self):
        with mock.patch.object(dashboard, "_write_json", return_value=False):
            self.assertFalse(dashboard._clear_butler_history())

    def test_crud_undo_removes_fields_added_by_update(self):
        handler = object.__new__(dashboard.DashboardHandler)
        reg = {"name": "关系", "file": "unused", "fields": {"who": "对象", "role": "角色"},
               "search": ["who"], "soft": True, "sub": None, "mirror": None}
        item = {"id": "r1", "who": "小明", "role": "朋友"}
        history = [{"op": "update", "entity": "关系",
                    "payload": {"id": "r1", "old": {"who": "小明", "role": None}}}]
        def read(path, default):
            return history if path == dashboard.UNDO_HISTORY_FILE else default
        with mock.patch.object(handler, "_crud_raw", return_value=[item]), \
             mock.patch.object(handler, "_crud_write", return_value=True), \
             mock.patch.object(dashboard, "CRUD_REGISTRY", [reg]), \
             mock.patch.object(dashboard, "_read_json", side_effect=read), \
             mock.patch.object(dashboard, "_write_json", return_value=True):
            result = handler.undo_action()
        self.assertTrue(result["success"])
        self.assertNotIn("role", item)

    def test_audit_fallback_ledger_is_isolated_from_home(self):
        # ZC-QA-1：对外 send_json 的 mutation 回执落账必须可注入临时路径；
        # 防止任何测试把回执写入 ~/.dash_mutations.json。
        import io as _io
        original = dashboard._MUTATION_LEDGER_FILE
        captured = {}
        handler = object.__new__(dashboard.DashboardHandler)
        handler._mutation_ctx = ("api/tasks/create", "iso-key-1", {})
        handler.wfile = _io.BytesIO()
        handler.send_response = lambda status: captured.setdefault("status", status)
        handler.send_header = lambda k, v: None
        handler.end_headers = lambda: None
        handler.send_cors = lambda: None
        try:
            with tempfile.TemporaryDirectory() as td:
                ledger = os.path.join(td, "mutations.json")
                dashboard._MUTATION_LEDGER_FILE = ledger
                handler.send_json({"success": True})
                stored = dashboard._find_mutation_result("api/tasks/create", "iso-key-1")
                self.assertTrue(stored and stored.get("success"))
                self.assertTrue(os.path.exists(ledger), "回执必须真实落盘到临时账本")
                rows = json.loads(open(ledger, encoding="utf-8").read()) if os.path.exists(ledger) else []
                self.assertTrue(any(r.get("key") == "iso-key-1" for r in rows), "账本行必须包含本测试的 mutation 键")
        finally:
            dashboard._MUTATION_LEDGER_FILE = original

    def test_create_habit_surfaces_local_dimension_warning(self):
        handler = object.__new__(dashboard.DashboardHandler)
        created = {"result": {"structuredContent": {"result": {"id": "h1"}}}}
        listed = {"result": {"structuredContent": {"result": [{"id": "h1", "name": "晨读"}]}}}
        with mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[created, listed]), \
             mock.patch.object(dashboard, "_load_habit_dims", return_value={}), \
             mock.patch.object(dashboard, "_save_habit_dims", return_value=False), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.create_habit({"name": "晨读", "dimension": "智力"})
        self.assertTrue(result["success"])
        self.assertIn("warning", result)

    def test_create_habit_rejects_success_without_authoritative_readback(self):
        handler = object.__new__(dashboard.DashboardHandler)
        created = {"result": {"structuredContent": {"result": {"id": "h1"}}}}
        listed = {"result": {"structuredContent": {"result": []}}}
        with mock.patch.object(dashboard, "call_ticktick_mcp", side_effect=[created, listed, listed]), \
             mock.patch.object(dashboard.time, "sleep", return_value=None):
            result = handler.create_habit({"name": "晨读"})
        self.assertFalse(result["success"])
        self.assertIn("回读未验证", result["error"])

    def test_shadow_status_is_read_only_and_reports_sources(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with tempfile.TemporaryDirectory() as td:
            handler.LOCAL_STATE_FILE = os.path.join(td, "local.json")
            with open(handler.LOCAL_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)
            result = handler._shadow_status()
        self.assertEqual(result["mode"], "shadow")
        self.assertTrue(result["local_sources"][handler.LOCAL_STATE_FILE])

    def test_butler_action_idempotency_survives_restart_and_audit_is_redacted(self):
        handler = object.__new__(dashboard.DashboardHandler)
        original_file = dashboard._ACTION_AUDIT_FILE
        original_turns = dashboard._executed_turns
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard._ACTION_AUDIT_FILE = os.path.join(td, "actions.json")
                dashboard._executed_turns = dashboard.deque(maxlen=200)
                with mock.patch.object(handler, "_butler_act_core", return_value={"success": True, "msg": "ok"}) as core:
                    first = handler.butler_act({"kind": "建任务", "args": {"标题": "私人任务", "content": "不应回传"}, "clientTurnId": "turn-1"})
                    self.assertTrue(first["success"])
                    self.assertEqual(first["receipt"]["clientTurnId"], "turn-1")
                    dashboard._executed_turns.clear()  # 模拟进程重启后的内存丢失
                    second = handler.butler_act({"kind": "建任务", "args": {"标题": "私人任务"}, "clientTurnId": "turn-1"})
                self.assertTrue(second["dup"])
                core.assert_called_once()
                rows = dashboard._load_action_audit()
                self.assertEqual(rows[0]["status"], "pending")
                self.assertEqual(rows[-1]["duplicate"], True)
                self.assertEqual(rows[0]["args"]["content"], "[已隐藏]")
        finally:
            dashboard._ACTION_AUDIT_FILE = original_file
            dashboard._executed_turns = original_turns

    def test_pending_action_is_not_reexecuted_blindly(self):
        handler = object.__new__(dashboard.DashboardHandler)
        original_file = dashboard._ACTION_AUDIT_FILE
        original_turns = dashboard._executed_turns
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard._ACTION_AUDIT_FILE = os.path.join(td, "actions.json")
                dashboard._executed_turns = dashboard.deque(maxlen=200)
                dashboard._write_json(dashboard._ACTION_AUDIT_FILE, [{"clientTurnId": "pending-1", "status": "pending", "success": False}])
                with mock.patch.object(handler, "_butler_act_core") as core:
                    result = handler.butler_act({"kind": "建任务", "args": {"标题": "不可重复"}, "clientTurnId": "pending-1"})
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                core.assert_not_called()
        finally:
            dashboard._ACTION_AUDIT_FILE = original_file
            dashboard._executed_turns = original_turns

    def test_generic_mutation_ledger_replays_success_without_raw_payload(self):
        original_file = dashboard._MUTATION_LEDGER_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard._MUTATION_LEDGER_FILE = os.path.join(td, "mutations.json")
                result = {"success": True, "task": {"id": "t1"}, "raw": {"secret": "omit"}}
                dashboard._store_mutation_result("/api/tasks/create", "m1", result)
                replay = dashboard._find_mutation_result("/api/tasks/create", "m1")
                self.assertTrue(replay["success"])
                self.assertNotIn("raw", replay)
                self.assertIsNone(dashboard._find_mutation_result("/api/tasks/create", "other"))
        finally:
            dashboard._MUTATION_LEDGER_FILE = original_file

    def test_standard_mutation_receipt_is_payload_free_and_verified(self):
        receipt = dashboard._standard_mutation_receipt(
            "/api/tasks/create", "m-test", {"success": True, "task": {"id": "t1"}},
            {"title": "私人标题"})
        self.assertEqual(receipt["domain"], "tasks")
        self.assertEqual(receipt["source"], "TickTick")
        self.assertEqual(receipt["entityId"], "t1")
        self.assertTrue(receipt["verified"])
        self.assertNotIn("私人标题", json.dumps(receipt, ensure_ascii=False))

    def test_critical_json_write_creates_restorable_snapshot(self):
        original_relations = dashboard.RELATIONS_FILE
        original_backup = dashboard.DASH_RECOVERY_BACKUP_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, "relations.json")
                dashboard.RELATIONS_FILE = path
                dashboard.DASH_RECOVERY_BACKUP_DIR = os.path.join(td, "recovery")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump([{"id": "old"}], f)
                self.assertTrue(dashboard._write_json(path, [{"id": "new"}]))
                status = dashboard._recovery_status()
                item = next(x for x in status["items"] if x["key"] == "relations")
                self.assertTrue(item["valid"])
                self.assertEqual(item["backupCount"], 1)
                restored = dashboard._restore_json_snapshot("relations", item["latestBackup"])
                self.assertTrue(restored["success"])
                self.assertTrue(restored["verified"])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), [{"id": "old"}])
                with open(path, "w", encoding="utf-8") as f:
                    f.write("{broken")
                self.assertEqual(dashboard._read_json(path, []), [])
                recovered = dashboard._restore_json_snapshot("relations", item["latestBackup"])
                self.assertTrue(recovered["success"])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), [{"id": "old"}])
        finally:
            dashboard.RELATIONS_FILE = original_relations
            dashboard.DASH_RECOVERY_BACKUP_DIR = original_backup

    def test_startup_recovery_baseline_is_created_once(self):
        original_backup = dashboard.DASH_RECOVERY_BACKUP_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                path = os.path.join(td, "domain.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"safe": True}, f)
                dashboard.DASH_RECOVERY_BACKUP_DIR = os.path.join(td, "recovery")
                with mock.patch.object(dashboard, "_critical_json_registry", return_value={
                    "domain": {"path": path, "label": "测试域"}
                }):
                    self.assertEqual(dashboard._ensure_recovery_baselines(), ["domain"])
                    self.assertEqual(dashboard._ensure_recovery_baselines(), [])
                    status = dashboard._recovery_status()["items"][0]
                    self.assertEqual(status["backupCount"], 1)
        finally:
            dashboard.DASH_RECOVERY_BACKUP_DIR = original_backup

    def test_empty_advisor_outcomes_domain_is_initialized_for_recovery(self):
        original = dashboard.ADVISOR_OUTCOMES_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.ADVISOR_OUTCOMES_FILE = os.path.join(td, "outcomes.json")
                self.assertEqual(dashboard._ensure_recovery_domain_files(), ["advisor-outcomes"])
                self.assertEqual(dashboard._ensure_recovery_domain_files(), [])
                with open(dashboard.ADVISOR_OUTCOMES_FILE, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), [])
        finally:
            dashboard.ADVISOR_OUTCOMES_FILE = original

    def test_ops_status_exposes_only_safe_release_and_counts(self):
        original_dir = dashboard.DASHBOARD_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.DASHBOARD_DIR = td
                with open(os.path.join(td, ".deploy-status.json"), "w", encoding="utf-8") as f:
                    json.dump({"release": "r1", "deployed_at": "2026-08-31T01:02:03+08:00",
                               "status": "healthy", "rollback_available": True,
                               "secret": "must-not-leak"}, f)
                with mock.patch.object(dashboard, "_recovery_status", return_value={"items": [
                    {"exists": True, "valid": True, "backupCount": 1},
                    {"exists": True, "valid": False, "backupCount": 0},
                ]}):
                    result = dashboard._ops_status()
                self.assertEqual(result["deploy"]["release"], "r1")
                self.assertNotIn("secret", result["deploy"])
                self.assertEqual(result["recovery"], {"domains": 2, "withBackups": 1, "invalid": 1})
                self.assertEqual(set(result["weeklyDraft"]["lastRun"]),
                                 {"started_at", "finished_at", "status", "exit_code"})
        finally:
            dashboard.DASHBOARD_DIR = original_dir

    def test_health_quality_marks_stale_and_out_of_range_without_mutating_values(self):
        today = dashboard.date.today().isoformat()
        data = {"days": [today], "sleep": [{"date": today, "total": 30}],
                "hrv": [{"date": "2000-01-01", "qty": 900}], "rhr": [], "resp": []}
        quality = dashboard.DashboardHandler._health_quality(data)
        self.assertTrue(quality["has_data"])
        self.assertEqual(quality["invalid"]["sleep"], 1)
        self.assertEqual(quality["invalid"]["hrv"], 1)
        self.assertEqual(quality["stale"][0]["metric"], "hrv")
        self.assertEqual(data["sleep"][0]["total"], 30)

    def test_health_ingest_status_reports_https_without_credentials(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with mock.patch.object(dashboard, "scan_ingest", return_value={"status": "ok"}):
            result = handler._health_ingest_status()
        self.assertTrue(result["transport"]["secure"])
        self.assertTrue(result["transport"]["push_url"].startswith("https://"))
        self.assertNotIn("token", json.dumps(result, ensure_ascii=False).lower())

    def test_advisor_outcomes_store_labels_without_prompt_or_answer(self):
        original = dashboard.ADVISOR_OUTCOMES_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.ADVISOR_OUTCOMES_FILE = os.path.join(td, "outcomes.json")
                payload = {"adviceId": "adv-0123456789abcdefabcd", "scene": "health",
                           "verdict": "useful", "prompt": "private question", "answer": "private answer"}
                result = dashboard._record_advisor_outcome(payload)
                self.assertTrue(result["success"])
                with open(dashboard.ADVISOR_OUTCOMES_FILE, encoding="utf-8") as f:
                    raw = f.read()
                self.assertNotIn("private question", raw)
                self.assertNotIn("private answer", raw)
                summary = dashboard._advisor_outcome_summary()
                self.assertEqual(summary["counts"]["useful"], 1)
                self.assertEqual(summary["usefulRate"], 1.0)
        finally:
            dashboard.ADVISOR_OUTCOMES_FILE = original

    def test_advisor_outcomes_reject_unknown_labels(self):
        result = dashboard._record_advisor_outcome({"adviceId": "adv-0123456789abcdefabcd",
                                                    "scene": "health", "verdict": "perfect"})
        self.assertFalse(result["success"])

    def test_advisor_prompt_enforces_fact_inference_recommendation_boundary(self):
        self.assertIn("事实边界", dashboard.ADVISOR_COMMON)
        self.assertIn("推断", dashboard.ADVISOR_COMMON)
        self.assertIn("建议", dashboard.ADVISOR_COMMON)
        self.assertIn("暂无证据", dashboard.ADVISOR_COMMON)

    def test_chain_health_builds_from_real_role_task_and_habit_shapes(self):
        handler = object.__new__(dashboard.DashboardHandler)
        roles = [{"id": "r1", "name": "身体", "key_results": [{"text": "每周骑行 2 次"}]},
                 {"id": "r2", "name": "学习者", "key_results": []}]
        tasks = [{"id": "t1", "title": "骑行", "content": "角色：身体\n象限：q2", "tags": []},
                 {"id": "t2", "title": "读书", "content": "象限：q2", "tags": []}]
        habits = [{"id": "h1", "name": "早睡", "dimension": "身体", "role": "身体"}]
        with mock.patch.object(dashboard, "load_obsidian_roles", return_value=roles), \
             mock.patch.object(dashboard, "load_ticktick_tasks", return_value=tasks), \
             mock.patch.object(dashboard, "cached", side_effect=lambda _key, fn: fn()), \
             mock.patch.object(dashboard, "_load_habit_dims", return_value={}), \
             mock.patch.object(dashboard, "_read_json", side_effect=lambda _path, default: default), \
             mock.patch.object(handler, "_get_habits", return_value={"habits": habits}), \
             mock.patch.object(handler, "_ppc_scores", return_value={"available": False}), \
             mock.patch.object(handler, "_correlation_tip", return_value=""):
            result = handler._get_dashboard_data()["chain_health"]
        self.assertEqual(result["rolesWithKR"], 1)
        self.assertEqual(result["rolesWithAction"], 1)
        self.assertEqual(result["tasksActionable"], 1)
        self.assertGreaterEqual(result["gapCount"], 2)

    def test_health_quality_context_explains_missing_stale_and_invalid_data(self):
        today = dashboard.date.today().isoformat()
        data = {
            "days": [today],
            "sleep": [{"date": today, "total": 7.0}],
            "hrv": [{"date": "2000-01-01", "qty": 900}],
            "quality": {
                "stale": [{"metric": "hrv", "days": 2}],
                "invalid": {"sleep": 1},
            },
        }
        text = dashboard.DashboardHandler._health_quality_context(data)
        self.assertIn("未收到", text)
        self.assertIn("陈旧", text)
        self.assertIn("异常", text)
        self.assertIn("HRV", text)

    def test_pack_rides_normalizes_units_and_keeps_gps_fallback(self):
        rides = {
            "metric": {"id": "metric", "name": "Cycling", "start": "2026-08-29T08:00:00+0800",
                       "duration": {"qty": 3600, "units": "s"},
                       "distance": {"qty": 5000, "units": "m"},
                       "speed": {"qty": 10, "units": "m/s"},
                       "intensity": {"qty": 1.5}},
            "route": {"id": "route", "name": "户外 骑行", "start": "2026-08-28T08:00:00+0800",
                      "duration": 1800,
                      "route": [{"latitude": 31.2304, "longitude": 121.4737},
                                {"latitude": 31.2314, "longitude": 121.4737}]},
        }
        packed, quality = dashboard.DashboardHandler._pack_rides(rides)
        metric = next(x for x in packed if x["date"] == "2026-08-29")
        fallback = next(x for x in packed if x["date"] == "2026-08-28")
        self.assertEqual(metric["distance_km"], 5.0)
        self.assertEqual(metric["duration_min"], 60.0)
        self.assertEqual(metric["speed_kmh"], 36.0)
        self.assertEqual(metric["distance_source"], "reported")
        self.assertGreater(fallback["distance_km"], 0)
        self.assertEqual(fallback["distance_source"], "gps")
        self.assertEqual(quality["distance"]["reported"], 1)
        self.assertEqual(quality["distance"]["gps"], 1)
        self.assertEqual(fallback["speed_source"], "derived")

    def test_pack_rides_marks_distance_missing_without_route(self):
        packed, quality = dashboard.DashboardHandler._pack_rides({
            "missing": {"id": "missing", "name": "户外 骑行", "start": "2026-08-30T08:00:00+0800",
                        "duration": 600, "distance": None, "route": []}
        })
        self.assertIsNone(packed[0]["distance_km"])
        self.assertEqual(packed[0]["distance_source"], "missing")
        self.assertEqual(quality["distance"]["missing"], 1)

    def test_pack_rides_handles_nested_route_units_and_workout_heart_rate(self):
        packed, quality = dashboard.DashboardHandler._pack_rides({
            "w": {"id": "w", "name": "Cycling", "start": "2026-08-30T08:00:00+0800",
                  "duration": {"qty": "2", "units": "h"},
                  "distance": {"qty": "10", "units": "km"},
                  "activeEnergyBurned": {"qty": 418.4, "units": "kcal"},
                  "elevationUp": {"qty": 328, "units": "ft"},
                  "heartRate": {"avg": 145, "max": 181}, "indoor": False}
        })
        row = packed[0]
        self.assertEqual(row["duration_min"], 120.0)
        self.assertEqual(row["distance_km"], 10.0)
        self.assertEqual(row["speed_source"], "derived")
        self.assertEqual(row["speed_kmh"], 5.0)
        self.assertEqual(row["kcal"], 418)
        self.assertEqual(row["energy_source"], "reported_kcal")
        self.assertEqual(row["elevation_up_m"], 100.0)
        self.assertEqual(row["heart_rate_avg"], 145)
        self.assertEqual(row["heart_rate_max"], 181)
        self.assertEqual(row["location"], "户外")
        self.assertEqual(quality["energy"]["reported_kcal"], 1)

    def test_pack_rides_treats_bare_hae_energy_as_kj(self):
        packed, quality = dashboard.DashboardHandler._pack_rides({
            "w": {"id": "w", "name": "Cycling", "start": "2026-08-30T08:00:00+0800",
                  "activeEnergyBurned": 418.4}
        })
        self.assertEqual(packed[0]["kcal"], 100)
        self.assertEqual(packed[0]["energy_source"], "reported_kj")
        self.assertEqual(quality["energy"]["reported_kj"], 1)

    def test_pack_rides_rejects_bad_accuracy_and_large_gps_jump(self):
        packed, quality = dashboard.DashboardHandler._pack_rides({
            "w": {"id": "w", "name": "户外 骑行", "start": "2026-08-30T08:00:00+0800",
                  "route": {"locations": [
                      {"latitude": 31.2304, "longitude": 121.4737, "horizontalAccuracy": 10},
                      {"latitude": 31.2314, "longitude": 121.4737, "horizontalAccuracy": 10},
                      {"latitude": 35.0, "longitude": 121.0, "horizontalAccuracy": 10},
                      {"latitude": 31.2314, "longitude": 121.4737, "horizontalAccuracy": 200},
                  ]}}
        })
        self.assertGreater(packed[0]["distance_km"], 0)
        self.assertLess(packed[0]["distance_km"], 1)
        self.assertEqual(packed[0]["distance_source"], "gps")
        self.assertEqual(quality["distance"]["gps"], 1)

    def test_health_extra_metrics_preserve_units_and_normalize_energy(self):
        want = {"2026-08-30"}
        handler = object.__new__(dashboard.DashboardHandler)
        packed = handler._health_pack({"2026-08-30": {
            "basal_energy_burned": {"qty": 418.4, "units": "kJ"},
            "walking_speed": {"qty": 4.752, "units": "km/hr"},
            "environmental_audio_exposure": {"qty": 59.2, "units": "dBASPL"},
        }}, want, "vps")
        self.assertAlmostEqual(packed["basal_energy_burned"][0]["qty"], 100.0, places=1)
        self.assertEqual(packed["basal_energy_burned"][0]["units"], "kcal")
        self.assertEqual(packed["walking_speed"][0]["units"], "km/h")
        self.assertEqual(packed["environmental_audio_exposure"][0]["units"], "dB")

    def test_health_pack_normalizes_metric_units_and_marks_sources(self):
        handler = object.__new__(dashboard.DashboardHandler)
        packed = handler._health_pack({"2026-08-30": {
            "active_energy": {"qty": 418.4, "units": "kJ"},
            "walking_running_distance": {"qty": 1500, "units": "m"},
            "apple_exercise_time": {"qty": 3600, "units": "s"},
        }}, {"2026-08-30"}, "vps")
        self.assertEqual(packed["energy"][0]["qty"], 100.0)
        self.assertEqual(packed["energy"][0]["units"], "kcal")
        self.assertEqual(packed["energy"][0]["source"], "reported")
        self.assertEqual(packed["dist"][0]["qty"], 1.5)
        self.assertEqual(packed["dist"][0]["units"], "km")
        self.assertEqual(packed["exercise"][0]["qty"], 60.0)
        self.assertEqual(packed["exercise"][0]["units"], "min")

    def test_health_empty_payload_keeps_stable_metric_schema(self):
        handler = object.__new__(dashboard.DashboardHandler)
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(dashboard, "ON_VPS", True), \
             mock.patch.object(handler, "_health_ingest_status", return_value={"status": "empty"}), \
             mock.patch.object(dashboard.os.path, "expanduser", side_effect=lambda p: td if "health-data/raw" in p else p):
            result = handler._health_from_local(7)
        for key in ("sleep", "heart", "energy", "exercise", "mindful", "hrv", "rhr",
                    "spo2", "resp", "steps", "dist", "workouts", "workout_quality"):
            self.assertIn(key, result)

    def test_training_load_uses_calendar_windows_and_receives_workouts(self):
        days = ["2026-08-%02d" % i for i in range(20, 30)]
        data = {"days": days, "workouts": [
            {"date": "2026-08-29", "duration_min": 60, "intensity": 2},
            {"date": "2026-08-22", "duration_min": 30, "intensity": 2},
        ]}
        derived = dashboard.DashboardHandler._health_derived(data)
        self.assertEqual(derived["training_load_7d"], 120.0)
        self.assertEqual(derived["training_load_28d"], 180.0)

    def test_health_advisor_context_includes_fitness_data_without_unbound_error(self):
        handler = object.__new__(dashboard.DashboardHandler)
        data = {
            "days": ["2026-08-29", "2026-08-30"],
            "sleep": [{"date": "2026-08-30", "total": 7.2}],
            "vo2_max": [{"date": "2026-08-30", "qty": 42.1}],
            "workouts": [{"date": "2026-08-29", "distance_km": 80.0, "duration_min": 180}],
            "derived": {"training_load_7d": 360.0, "training_load_28d": 720.0},
            "health_fetched_at": "2026-08-30 18:00",
            "workout_fetched_at": "2026-08-30 17:46",
        }
        with mock.patch.object(handler, "_get_health", return_value=data):
            context = handler._health_advisor_context()
        self.assertIn("VO₂ Max 42.1", context)
        self.assertIn("近14天骑行 80.0km/180分钟", context)
        self.assertIn("近7天 360.0 AU", context)
        self.assertIn("健康 2026-08-30 18:00", context)

    def test_health_advisor_context_works_without_sleep_when_other_health_data_exists(self):
        handler = object.__new__(dashboard.DashboardHandler)
        data = {"hrv": [{"date": "2026-08-29", "qty": 30}, {"date": "2026-08-30", "qty": 35}]}
        with mock.patch.object(handler, "_get_health", return_value=data):
            context = handler._health_advisor_context()
        self.assertIn("HRV 35ms", context)
        self.assertIn("数据质量", context)

    def test_health_advisor_context_marks_unobserved_metrics_and_ingest_time(self):
        handler = object.__new__(dashboard.DashboardHandler)
        data = {
            "source": "vps",
            "days": ["2026-08-29", "2026-08-30"],
            "hrv": [{"date": "2026-08-29", "qty": 30}, {"date": "2026-08-30", "qty": 35}],
            "workouts": [{"date": "2026-08-29", "distance_km": 80.0, "duration_min": 180}],
            "ingest": {
                "metric_names": ["heart_rate_variability"],
                "metric_latest_data_date": {"heart_rate_variability": "2026-08-30"},
                "latest_receive_at": "2026-08-30T18:00:00+08:00",
            },
        }
        with mock.patch.object(handler, "_get_health", return_value=data):
            context = handler._health_advisor_context()
        self.assertIn("静息心率（HAE 未推送）", context)
        self.assertIn("呼吸率（HAE 未推送）", context)
        self.assertIn("原始推送 2026-08-30T18:00", context)

    def test_health_fingerprint_changes_when_new_workout_arrives(self):
        handler = object.__new__(dashboard.DashboardHandler)
        base = {"sleep": [{"date": "2026-08-30", "total": 7.0}],
                "workouts": [], "workout_fetched_at": "2026-08-30 10:00"}
        after = {"sleep": [{"date": "2026-08-30", "total": 7.0}],
                 "workouts": [{"date": "2026-08-30", "start": "08:00", "distance_km": 42.0,
                                "duration_min": 95}],
                 "workout_fetched_at": "2026-08-30 12:00"}
        with mock.patch.object(handler, "_get_health", side_effect=[base, after]):
            before_fp = handler._health_fingerprint()
            after_fp = handler._health_fingerprint()
        self.assertNotEqual(before_fp, after_fp)
        self.assertIn("workout=2026-08-30|08:00|42.00|95.0", after_fp)

    def test_shadow_status_returns_bounded_diff_details_without_payload(self):
        handler = object.__new__(dashboard.DashboardHandler)
        original_report = dashboard.SHADOW_REPORT_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                dashboard.SHADOW_REPORT_FILE = os.path.join(td, "shadow.json")
                with open(dashboard.SHADOW_REPORT_FILE, "w", encoding="utf-8") as f:
                    json.dump({"healthy": False, "entities": 3, "checked_at": "now",
                               "changed": [{"id": "x", "fields": ["title"], "payload": "secret"}],
                               "missing": ["m"], "extra": ["e"]}, f)
                result = handler._shadow_status()
                self.assertEqual(result["shadow_details"]["changed"][0], {"id": "x", "fields": ["title"]})
                self.assertNotIn("last_report", result)
        finally:
            dashboard.SHADOW_REPORT_FILE = original_report

    def test_shadow_reconcile_executes_fixed_read_only_script_with_cooldown(self):
        handler = object.__new__(dashboard.DashboardHandler)
        original_last = dashboard._shadow_reconcile_last
        try:
            dashboard._shadow_reconcile_last = 0
            with mock.patch.object(dashboard.os.path, "isfile", return_value=True), \
                 mock.patch.object(dashboard.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="{}", stderr="")) as run, \
                 mock.patch.object(handler, "_shadow_status", return_value={"mode": "shadow"}), \
                 mock.patch.object(dashboard, "_read_json", return_value={"ticktick_tasks": 8}):
                result = handler._shadow_reconcile()
            self.assertTrue(result["success"])
            self.assertFalse(result["cooldown"])
            self.assertTrue(result["ticktick_reachable"])
            self.assertEqual(result["ticktick_tasks"], 8)
            args = run.call_args.args[0]
            self.assertEqual(args[1], os.path.join(dashboard.DASHBOARD_DIR, "shadow_live_check.py"))
            with mock.patch.object(dashboard, "_read_json", return_value={"ticktick_tasks": 8}):
                result2 = handler._shadow_reconcile()
            self.assertTrue(result2["cooldown"])
            self.assertTrue(result2["ticktick_reachable"])
            self.assertEqual(result2["ticktick_tasks"], 8)
        finally:
            dashboard._shadow_reconcile_last = original_last

    def test_security_headers_are_defined_for_all_response_types(self):
        with open(os.path.join(ROOT, "dashboard-server.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn("def send_security_headers", source)
        self.assertGreaterEqual(source.count("self.send_security_headers()"), 5)
        self.assertIn("self.send_security_headers()", source)
        self.assertIn("self.send_response(204); self.send_cors(); self.send_security_headers()", source)
        self.assertIn('self.send_header("Content-Length", "0")', source)
        self.assertIn('self.send_header("Content-Length", str(len(body)))', source)

    def test_http_api_includes_security_headers(self):
        server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/healthz", timeout=3) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["service"], "dashboard")
            head = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/healthz", method="HEAD")
            with urllib.request.urlopen(head, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "application/json; charset=utf-8")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/shadow/status", timeout=3
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
                self.assertIn("camera=()", response.headers.get("Permissions-Policy", ""))
                self.assertEqual(response.headers.get("Vary"), "Origin")
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3) as response:
                etag = response.headers.get("ETag")
                self.assertTrue(etag)
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/", headers={"If-None-Match": etag}
            )
            try:
                urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 304)
                exc.close()
            else:
                self.fail("expected conditional request to return 304")
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/sw.js", timeout=3) as response:
                sw_etag = response.headers.get("ETag")
                self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
            sw_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/sw.js", headers={"If-None-Match": sw_etag}
            )
            try:
                urllib.request.urlopen(sw_request, timeout=3)
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 304)
                self.assertEqual(exc.headers.get("Cache-Control"), "no-cache")
                exc.close()
            else:
                self.fail("expected sw conditional request to return 304")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_get_handler_returns_server_error_status_on_unexpected_exception(self):
        with open(os.path.join(ROOT, "dashboard-server.py"), encoding="utf-8") as f:
            source = f.read()
        # ZC-QA-1：SSE 500 对外契约 = 稳定错误码 + 通用文案（详情只进服务端日志，原为裸 str(e)）
        self.assertIn('payload.update(_client_error(e)); self.send_json(payload, status=500)', source)
        for lineno, line in enumerate(source.splitlines(), 1):
            if '"error": str(' in line:
                self.fail(f"未脱敏错误直出 dashboard-server.py:{lineno}")

    def test_boot_payload_reads_slow_independent_domains_in_parallel(self):
        handler = dashboard.DashboardHandler.__new__(dashboard.DashboardHandler)
        barrier = threading.Barrier(4)

        def core(value):
            barrier.wait(timeout=2)
            return value

        handler._get_dashboard_data = lambda: core({"tasks": [], "roles": [], "chain_health": {"ok": True}})
        handler._get_stats = lambda: core({"ok": True})
        handler._get_week_report = lambda: core({"ok": True})
        handler._get_health = lambda days: core({"days": ["2026-09-03"]})
        handler._get_habits = lambda: {"habits": []}
        handler.get_relations = lambda: []
        handler.get_listening = lambda: []
        handler.get_proactive = lambda: {}
        handler.get_week_plan = lambda: {}

        with mock.patch.object(dashboard, "cached", side_effect=lambda _key, fn: fn()):
            payload = handler._get_boot_payload()

        self.assertNotIn("error", payload["dashboard"])
        self.assertNotIn("error", payload["health"])
        self.assertEqual(payload["chain_health"], {"ok": True})
        self.assertEqual(payload["habits"], {"habits": []})

    def test_butler_task_actions_expose_receipt_bound_undo(self):
        with open(os.path.join(ROOT, "dashboard-server.py"), encoding="utf-8") as f:
            source = f.read()
        self.assertIn('self.send_json(self.undo_action(data))', source)
        self.assertIn('"ticktick_archive"', source)
        self.assertIn('requested_turn', source)
        self.assertIn('"undoable"', source)
        self.assertIn('"entityId"', source)


if __name__ == "__main__":
    unittest.main()
