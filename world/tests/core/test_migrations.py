import sqlite3
import tempfile
import unittest
from pathlib import Path

import server.migrations as migrations


class MigrationRunnerTest(unittest.TestCase):
    def test_applies_once_and_records_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(str(Path(tmp) / 'state.db'))
            self.addCleanup(db.close)
            ran = migrations.apply(db)
            self.assertIn('0001_confirmation_proposals', ran)
            self.assertEqual(migrations.apply(db), [])
            count = db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
            self.assertEqual(count, 1)
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='confirmation_proposals'").fetchone()
            self.assertIsNotNone(table)

    def test_changed_history_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / '0001_test.sql'
            first.write_text('CREATE TABLE t (id INTEGER);', encoding='utf-8')
            db = sqlite3.connect(str(root / 'state.db'))
            self.addCleanup(db.close)
            migrations.apply(db, root)
            first.write_text('CREATE TABLE t (id INTEGER, changed TEXT);',
                             encoding='utf-8')
            with self.assertRaises(RuntimeError):
                migrations.apply(db, root)


if __name__ == '__main__':
    unittest.main()
