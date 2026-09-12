"""Synthetic privacy probes; no real credentials or network."""
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]

def function(name, scope):
    tree = ast.parse((ROOT / 'dashboard-server.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<privacy-probe>', 'exec'), scope)
    return scope[name]

class PrivacyBoundaries(unittest.TestCase):
    def test_host_allowlist_is_required_even_for_remote_mode(self):
        from urllib.parse import urlparse
        tree = ast.parse((ROOT / 'dashboard-server.py').read_text())
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_is_trusted_client')
        scope = {'os': os, 'urlparse': urlparse}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<host-probe>', 'exec'), scope)
        handler = Mock(headers={'Host': 'attacker.example:8787'}, client_address=('127.0.0.1', 1))
        trusted = scope['_is_trusted_client']
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(trusted(handler))
            handler.headers = {'Host': 'localhost:8787'}
            self.assertTrue(trusted(handler))
            handler.client_address = ('192.0.2.1', 1)
            self.assertFalse(trusted(handler))
        with patch.dict(os.environ, {'DASH_ALLOW_REMOTE': '1'}, clear=True):
            self.assertTrue(trusted(handler))
            handler.headers = {'Host': 'attacker.example'}
            self.assertFalse(trusted(handler))

    def test_home_credentials_are_not_inherited(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, '.dash_config').write_text('TICKTICK_TOKEN=SYNTHETIC_PRIVATE\n')
            with patch.dict(os.environ, {'HOME': td, 'DASH_DEMO': '0', 'DASH_CONFIG': ''}, clear=True):
                load = function('_load_config', {'os': os, '__file__': str(Path(td, 'project', 'server.py'))})
                self.assertEqual(load(), {})

    def test_demo_does_not_read_explicit_credentials(self):
        load = function('_load_config', {'os': os, '__file__': str(ROOT / 'dashboard-server.py')})
        with patch.dict(os.environ, {'DASH_DEMO': '1'}, clear=True), patch('builtins.open', side_effect=AssertionError('read')):
            self.assertEqual(load(), {})

    def test_profile_sync_requires_opt_in_and_non_demo(self):
        send = Mock(side_effect=AssertionError('network'))
        for env, demo in [({}, False), ({'DASH_PROFILE_SYNC': '1'}, True)]:
            scope = {'os': os, '_demo_enabled': lambda: demo, '_vps_get': send}
            sync = function('_sync_user_profile_with_vps', scope)
            with patch.dict(os.environ, env, clear=True):
                sync()
        send.assert_not_called()

    def test_usage_defaults_to_data_root(self):
        import ai_runtime
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'DASH_DATA_DIR': td}, clear=True):
            self.assertEqual(ai_runtime._ledger_path(), str(Path(td, 'ai_usage.json')))

    def test_cross_site_post_origin_rejected(self):
        """text/plain 简单请求可绕过 CORS 预检；写接口必须校验 Origin 同源/环回。"""
        import ast
        from urllib.parse import urlparse as up
        tree = ast.parse((ROOT / 'dashboard-server.py').read_text())
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_origin_allowed')
        scope = {'urlparse': up, 'os': os}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<origin-probe>', 'exec'), scope)
        allowed = scope['_origin_allowed']
        class H:
            headers = {}
        h = H()
        h.headers = {'Origin': 'https://evil.example', 'Host': '127.0.0.1:8787'}
        self.assertFalse(allowed(h))
        h.headers = {'Origin': 'http://127.0.0.1:8787', 'Host': '127.0.0.1:8787'}
        self.assertTrue(allowed(h))
        h.headers = {'Origin': 'http://localhost:8787', 'Host': 'localhost:8787'}
        self.assertTrue(allowed(h))
        h.headers = {'Origin': 'https://dash.example.com', 'Host': 'dash.example.com'}
        with patch.dict(os.environ, {'DASH_ALLOWED_ORIGINS': ''}):
            self.assertFalse(allowed(h))
        with patch.dict(os.environ, {'DASH_ALLOWED_ORIGINS': 'https://dash.example.com'}):
            self.assertTrue(allowed(h))
        h.headers = {'Origin': 'http://localhost:9999', 'Host': 'localhost:8787'}
        self.assertFalse(allowed(h))
        h.headers = {'Origin': 'https://evil.example', 'Host': 'dash.example.com'}
        self.assertFalse(allowed(h))
        h.headers = {}
        self.assertTrue(allowed(h))

    def test_do_post_enforces_origin_check(self):
        src = (ROOT / 'dashboard-server.py').read_text(encoding='utf-8')
        do_post = src.index('def do_POST')
        guard = src.index('if not self._origin_allowed()', do_post)
        trusted = src.index('if not self._is_trusted_client()', do_post)
        self.assertLess(trusted, guard, 'do_POST 必须先做环回校验，再做 Origin 校验')
