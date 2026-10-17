"""Requirement lifecycle, migration and database compatibility regression checks."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service import AppError, SCHEMA_VERSION, Store
import database


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'requirements.db')
        self.store = Store(self.path)
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')
        self.project = self.store.create_project('Project')
        self.requirement = self.store.create_requirement(self.project['id'], 'Original', 'Description', self.admin['id'])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def archive(self):
        return self.store.archive_requirement(self.project['id'], self.requirement['id'], self.requirement['version'])

    def test_edit_preserves_creator_and_time(self):
        updated = self.store.update_requirement(self.project['id'], self.requirement['id'], {'title': ' Updated ', 'description': '', 'version': 1})
        self.assertEqual((updated['title'], updated['description'], updated['version']), ('Updated', '', 2))
        self.assertEqual(updated['created_by'], self.requirement['created_by'])
        self.assertEqual(updated['created_at'], self.requirement['created_at'])

    def test_stale_edit_and_missing_version(self):
        archived = self.archive()
        with self.assertRaises(AppError) as stale:
            self.store.update_requirement(self.project['id'], self.requirement['id'], {'title': 'Stale', 'version': 1})
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(AppError) as missing:
            self.store.restore_requirement(self.project['id'], self.requirement['id'])
        self.assertEqual(missing.exception.status, 428)
        self.assertEqual(self.store.get_requirement(self.project['id'], self.requirement['id']), archived)

    def test_archive_restore_and_filter(self):
        archived = self.archive()
        self.assertIs(archived['archived'], True)
        self.assertEqual(self.store.list_requirements(self.project['id'], include_archived=False), [])
        self.assertEqual(len(self.store.list_requirements(self.project['id'])), 1)
        self.assertEqual(self.store.archive_requirement(self.project['id'], archived['id'], archived['version']), archived)
        with self.assertRaises(AppError):
            self.store.update_requirement(self.project['id'], archived['id'], {'title': 'No', 'version': archived['version']})
        restored = self.store.restore_requirement(self.project['id'], archived['id'], archived['version'])
        self.assertFalse(restored['archived'])
        self.assertEqual(restored['version'], 3)
        self.assertEqual(len(self.store.list_requirements(self.project['id'], include_archived=False)), 1)

    def test_cross_project_operations_are_rejected(self):
        other = self.store.create_project('Other')
        for operation in (
            lambda: self.store.update_requirement(other['id'], self.requirement['id'], {'title': 'No', 'version': 1}),
            lambda: self.store.archive_requirement(other['id'], self.requirement['id'], 1),
            lambda: self.store.restore_requirement(other['id'], self.requirement['id'], 1),
        ):
            with self.assertRaises(AppError) as denied:
                operation()
            self.assertEqual(denied.exception.status, 404)

    def test_old_tasks_can_change_and_new_links_are_blocked(self):
        old = self.store.create_task(self.project['id'], 'Old task', self.requirement['id'], None, None, self.admin['id'])
        unlinked = self.store.create_task(self.project['id'], 'Unlinked task', None, None, None, self.admin['id'])
        self.archive()
        with self.assertRaises(AppError):
            self.store.create_task(self.project['id'], 'New task', self.requirement['id'], None, None, self.admin['id'])
        with self.assertRaises(AppError):
            self.store.update_task(self.project['id'], unlinked['id'], {'requirement_id': self.requirement['id'], 'version': unlinked['version']})
        updated = self.store.update_task(self.project['id'], old['id'], {'status': 'done', 'requirement_id': str(self.requirement['id']), 'due_date': '2026-10-20', 'version': old['version']})
        self.assertEqual(updated['status'], 'done')
        self.assertEqual(updated['requirement_id'], self.requirement['id'])
        cleared = self.store.update_task(self.project['id'], old['id'], {'requirement_id': None, 'version': updated['version']})
        self.assertIsNone(cleared['requirement_id'])
        with self.assertRaises(AppError):
            self.store.update_task(self.project['id'], old['id'], {'requirement_id': self.requirement['id'], 'version': cleared['version']})

    def test_restore_allows_new_link(self):
        archived = self.archive()
        self.store.restore_requirement(self.project['id'], self.requirement['id'], archived['version'])
        task = self.store.create_task(self.project['id'], 'Restored link', self.requirement['id'], None, None, self.admin['id'])
        self.assertEqual(task['requirement_id'], self.requirement['id'])

    def test_invalid_fields_do_not_change_requirement(self):
        for changes in ({'title': {}, 'version': 1}, {'description': None, 'version': 1},
                        {'description': 'x' * 501, 'version': 1}, {'title': '', 'version': 1},
                        {'archived': True, 'version': 1}, {'title': 'No', 'version': True}, {'version': 1}):
            with self.subTest(changes=changes), self.assertRaises(AppError):
                self.store.update_requirement(self.project['id'], self.requirement['id'], changes)
        self.assertEqual(self.store.get_requirement(self.project['id'], self.requirement['id']), self.requirement)

    def test_two_connections_reject_stale_archive(self):
        other = Store(self.path)
        try:
            self.store.update_requirement(self.project['id'], self.requirement['id'], {'description': 'New description', 'version': 1})
            with self.assertRaises(AppError) as stale:
                other.archive_requirement(self.project['id'], self.requirement['id'], 1)
            self.assertEqual(stale.exception.status, 409)
        finally:
            other.close()

    def test_archived_between_validation_and_insert_is_rejected(self):
        other = Store(self.path)
        original = self.store._requirement_link
        def race(*args):
            value = original(*args)
            other.archive_requirement(self.project['id'], self.requirement['id'], 1)
            return value
        try:
            with patch.object(self.store, '_requirement_link', side_effect=race), self.assertRaises(AppError) as denied:
                self.store.create_task(self.project['id'], 'Race', self.requirement['id'], None, None, self.admin['id'])
            self.assertEqual(denied.exception.status, 409)
            self.assertEqual(self.store.list_tasks(self.project['id']), [])
        finally:
            other.close()

    def test_archived_between_validation_and_task_relink_is_rejected(self):
        task = self.store.create_task(self.project['id'], 'Unlinked', None, None, None, self.admin['id'])
        other = Store(self.path)
        original = self.store._requirement_link
        def race(*args):
            value = original(*args)
            other.archive_requirement(self.project['id'], self.requirement['id'], 1)
            return value
        try:
            with patch.object(self.store, '_requirement_link', side_effect=race), self.assertRaises(AppError) as denied:
                self.store.update_task(self.project['id'], task['id'], {'requirement_id': self.requirement['id'], 'version': task['version']})
            self.assertEqual(denied.exception.status, 409)
            self.assertEqual(self.store.get_task(self.project['id'], task['id']), task)
        finally:
            other.close()

    def test_legacy_migration_preserves_tasks_and_requirement(self):
        self.store.create_task(self.project['id'], 'Historic task', self.requirement['id'], None, None, self.admin['id'])
        with self.store.db:
            self.store.db.execute('DROP INDEX idx_requirements_project_archived')
            self.store.db.execute('ALTER TABLE requirements DROP COLUMN archived')
            self.store.db.execute('ALTER TABLE requirements DROP COLUMN version')
        self.store.close()
        self.store = Store(self.path)
        migrated = self.store.get_requirement(self.project['id'], self.requirement['id'])
        self.assertEqual(migrated, self.requirement)
        self.assertEqual(len(self.store.list_tasks(self.project['id'])), 1)
        backup = self.path + '.pre-requirements.bak'
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute('SELECT title FROM requirements').fetchone()[0], 'Original')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_requirement(self.project['id'], self.requirement['id']), migrated)


class DatabaseUpdateTests(unittest.TestCase):
    def test_database_module_supports_memory_connection(self):
        with closing(database.open_database(':memory:')) as connection:
            self.assertIs(connection.row_factory, sqlite3.Row)
            self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    def test_failed_initialization_closes_connection(self):
        connection = sqlite3.connect(':memory:')
        with patch.object(database.sqlite3, 'connect', return_value=connection), \
                patch.object(database, '_initialize', side_effect=RuntimeError('test failure')):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                database.open_database(':memory:')
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')

    def test_schema_version_and_idempotent_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'db.sqlite')
            store = Store(path)
            store.setup('member@example.test', 'MemberPass123')
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            store.close()
            store = Store(path)
            try:
                self.assertFalse(store.needs_setup())
                self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            finally:
                store.close()

    def test_future_schema_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'future.sqlite')
            with closing(sqlite3.connect(path)) as future:
                future.execute(f'PRAGMA user_version={SCHEMA_VERSION + 1}')
                future.execute('CREATE TABLE future_data(value TEXT)')
                future.execute("INSERT INTO future_data VALUES('preserve')")
                future.commit()
            with self.assertRaises(AppError) as denied:
                Store(path)
            self.assertEqual(denied.exception.status, 409)
            with closing(sqlite3.connect(path)) as future:
                self.assertEqual(future.execute('SELECT value FROM future_data').fetchone()[0], 'preserve')
                self.assertEqual(future.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='users'").fetchone()[0], 0)

    def test_role_listing_is_one_query_and_keeps_empty_roles(self):
        store = Store(':memory:')
        try:
            empty = store.create_role('Empty')
            for i in range(20):
                role = store.create_role(f'Role {i}')
                store.set_permissions(role['id'], ['members.manage', 'workitems.manage'])
            queries = []
            store.db.set_trace_callback(queries.append)
            result = store.list_roles()
            self.assertEqual(len([q for q in queries if q.lstrip().upper().startswith('SELECT')]), 1)
            self.assertEqual(result[0], empty)
            self.assertEqual(result[1]['permissions'], ['members.manage', 'project.view', 'workitems.manage'])
            self.assertEqual(len(result), 21)
        finally:
            store.close()

    def test_project_scoped_queries_have_usable_indexes(self):
        store = Store(':memory:')
        try:
            statements = [
                ('SELECT MAX(position) FROM tasks WHERE project_id=? AND status=?', (1, 'todo'), 'idx_tasks_project_status_position'),
                ('SELECT id FROM tasks WHERE project_id=? AND assignee_user_id=?', (1, 2), 'idx_tasks_project_assignee'),
                ('SELECT role_id FROM memberships WHERE user_id=?', (1,), 'idx_memberships_user'),
                ('SELECT id FROM requirements WHERE project_id=? AND archived=0 ORDER BY id', (1,), 'idx_requirements_project_archived'),
            ]
            for query, params, index in statements:
                with self.subTest(index=index):
                    plan = ' '.join(str(row['detail']) for row in store.db.execute('EXPLAIN QUERY PLAN ' + query, params))
                    self.assertIn(index, plan)
        finally:
            store.close()



if __name__ == '__main__':
    unittest.main(verbosity=2)
