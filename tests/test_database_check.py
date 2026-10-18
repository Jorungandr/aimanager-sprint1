"""Read-only diagnostics against isolated database files."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import SCHEMA_VERSION
from scripts.check_database import inspect_database, main
from service import Store


class DatabaseCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'database #1.sqlite'
        store = Store(str(self.path))
        self.admin = store.setup('private@example.test', 'PrivatePass123')
        self.project = store.create_project('Private project', creator=self.admin)
        self.requirement = store.create_requirement(self.project['id'], 'Private title', '', self.admin['id'])
        self.task = store.create_task(self.project['id'], 'Private task', self.requirement['id'],
                                      self.admin['id'], None, self.admin['id'])
        store.close()

    def run_check(self, expected_exit):
        before = self.path.read_bytes()
        files = set(self.path.parent.iterdir())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(self.path)]), expected_exit)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(self.path.parent.iterdir()), files)
        for secret in ('private@example.test', 'PrivatePass123', 'Private title', 'Private task'):
            self.assertNotIn(secret, output.getvalue())
        return json.loads(output.getvalue())

    def test_healthy_owner_assignment_and_read_only_connection(self):
        connect = sqlite3.connect
        def readonly_connect(*args, **kwargs):
            self.assertIn('mode=ro', args[0])
            connection = connect(*args, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('CREATE TABLE forbidden(value TEXT)')
            return connection
        with patch('scripts.check_database.sqlite3.connect', side_effect=readonly_connect):
            report = self.run_check(0)
        self.assertEqual(report, {'schema_version': SCHEMA_VERSION, 'findings': []})

    def test_business_inconsistencies_and_foreign_keys_are_reported(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO projects(id,name) VALUES(2,'Other')")
            db.execute('UPDATE requirements SET project_id=2')
            db.execute('UPDATE tasks SET assignee_user_id=999')
        report = self.run_check(1)
        categories = {item['category']: item for item in report['findings']}
        self.assertEqual(set(categories), {'cross_project_requirement', 'invalid_assignee', 'foreign_keys'})
        for item in categories.values():
            self.assertEqual(item['count'], 1)
            self.assertEqual(item['record_ids'], [self.task['id']])

    def test_legacy_and_future_versions_are_not_migrated(self):
        for version in (0, SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(f'PRAGMA user_version={version}')
                report = self.run_check(1)
                self.assertEqual(report['findings'][0]['category'], 'schema_version')

    def test_missing_columns_are_reported_without_crash(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP INDEX idx_requirements_project_archived')
            db.execute('ALTER TABLE requirements DROP COLUMN archived')
        report = self.run_check(1)
        self.assertEqual(report['findings'], [{'category': 'schema_columns', 'table': 'requirements', 'count': 1}])

    def test_missing_path_does_not_create_file(self):
        missing = self.path.parent / 'missing.sqlite'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(missing)]), 2)
        self.assertFalse(missing.exists())

    def test_normal_member_assignment_is_healthy(self):
        with closing(Store(str(self.path))) as store:
            member = store.create_user('member@example.test', 'MemberPass123')
            role = store.create_role('Contributor')
            store.set_permissions(role['id'], ['workitems.manage'], actor=self.admin)
            store.add_member(self.project['id'], member['id'], role['id'], actor=self.admin)
            task = store.update_task(self.project['id'], self.task['id'], {
                'assignee_user_id': member['id'], 'version': self.task['version'],
            })
            self.assertEqual(task['assignee_user_id'], member['id'])
        self.assertEqual(self.run_check(0)['findings'], [])

    def test_archived_requirement_keeps_valid_historic_link(self):
        with closing(Store(str(self.path))) as store:
            archived = store.archive_requirement(self.project['id'], self.requirement['id'],
                                                  self.requirement['version'])
            self.assertTrue(archived['archived'])
            self.assertEqual(store.get_task(self.project['id'], self.task['id'])['requirement_id'],
                             self.requirement['id'])
        self.assertEqual(self.run_check(0)['findings'], [])

    def test_findings_count_all_rows_but_limit_identifiers_to_100(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO projects(id,name) VALUES(2,'Other')")
            db.execute('UPDATE requirements SET project_id=2')
            db.execute('UPDATE tasks SET assignee_user_id=999')
            db.executemany('''INSERT INTO tasks(project_id,requirement_id,title,assignee_user_id,created_by)
                VALUES(?,?,?,?,?)''', [(self.project['id'], self.requirement['id'], 'Private task',
                                        999, self.admin['id']) for _ in range(100)])
            ids = {row[0] for row in db.execute('SELECT id FROM tasks')}
        report = self.run_check(1)
        self.assertEqual({item['category'] for item in report['findings']},
                         {'foreign_keys', 'cross_project_requirement', 'invalid_assignee'})
        for finding in report['findings']:
            with self.subTest(category=finding['category']):
                self.assertEqual(finding['count'], 101)
                self.assertEqual(len(finding['record_ids']), 100)
                self.assertEqual(len(set(finding['record_ids'])), 100)
                self.assertTrue(set(finding['record_ids']).issubset(ids))

    def test_real_cli_json_and_exit_codes_do_not_change_files(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'check_database.py'
        missing = self.path.parent / 'missing-cli.sqlite'
        for expected_exit in (0, 1, 2):
            with self.subTest(exit_code=expected_exit):
                if expected_exit == 1:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute('UPDATE tasks SET assignee_user_id=999')
                before = {path.name: path.read_bytes() for path in self.path.parent.iterdir()}
                target = missing if expected_exit == 2 else self.path
                result = subprocess.run([sys.executable, str(script), str(target)],
                                        cwd=self.path.parent, capture_output=True, text=True,
                                        encoding='utf-8', timeout=15, check=False)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertEqual(result.stderr, '')
                report = json.loads(result.stdout)
                if expected_exit == 0:
                    self.assertEqual(report['findings'], [])
                elif expected_exit == 1:
                    self.assertTrue(report['findings'])
                else:
                    self.assertIsInstance(report['error'], str)
                self.assertNotIn('private@example.test', result.stdout)
                self.assertEqual({path.name: path.read_bytes() for path in self.path.parent.iterdir()}, before)
        self.assertFalse(missing.exists())

    def test_invalid_database_and_directory_fail_safely(self):
        self.path.write_bytes(b'not a database with private@example.test')
        self.assertIn('error', self.run_check(2))
        with self.assertRaises(ValueError):
            inspect_database(self.path.parent)


if __name__ == '__main__':
    unittest.main(verbosity=2)
