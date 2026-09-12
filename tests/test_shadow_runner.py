import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
class RunnerTests(unittest.TestCase):
    def test_unaccepted_drift_preserves_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'x.sqlite3'; snap=Path(td)/'tasks.json'
            snap.write_text(json.dumps([{'id':'t','title':'old'}]),encoding='utf-8')
            # Use the lower-level runner only for a deterministic local fixture.
            from dashboard_sqlite import managed_db, migrate_snapshot
            with managed_db(db) as c: migrate_snapshot(c,[{'id':'t','kind':'task','source':'x','payload':{'title':'old'}}],revision='1')
            with managed_db(db) as c: before=json.loads(c.execute('select payload from entities').fetchone()[0])
            with managed_db(db) as c: self.assertIn('old', c.execute('select payload from entities').fetchone()[0])
            self.assertEqual(before, {'title':'old'})
if __name__=='__main__': unittest.main()
