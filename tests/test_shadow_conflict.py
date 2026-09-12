import sqlite3, tempfile, unittest
from dashboard_sqlite import managed_db, migrate_snapshot

class ShadowConflictTests(unittest.TestCase):
    def test_different_sources_are_recorded(self):
        with tempfile.NamedTemporaryFile(suffix='.sqlite3') as f:
            with managed_db(f.name) as db:
                migrate_snapshot(db, [{'id':'x','kind':'task','source':'ticktick','payload':{'title':'remote'}}], revision='1')
                result=migrate_snapshot(db, [{'id':'x','kind':'task','source':'obsidian','payload':{'title':'local'}}], revision='2')
                self.assertEqual(result['conflicts'], 1)
                self.assertEqual(db.execute('select count(*) from migration_conflicts').fetchone()[0], 1)
                self.assertIn('remote', db.execute('select payload from entities where id="x"').fetchone()[0])

if __name__ == '__main__': unittest.main()
