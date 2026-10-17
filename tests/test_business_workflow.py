"""Cross-feature business workflows; not HTTP or human acceptance tests."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import AppError, Store


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / 'workflow.sqlite')
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')
        self.member = self.store.create_user('member@example.test', 'MemberPass123')
        self.role = self.store.create_role('Contributor')
        self.store.set_permissions(self.role['id'], ['workitems.manage'], actor=self.admin)
        self.project = self.store.create_project('Sprint workflow', creator=self.admin)
        self.project_id = self.project['id']
        self.store.add_member(self.project_id, self.member['id'], self.role['id'], actor=self.admin)
        self.requirement = self.store.create_requirement(
            self.project_id, 'Delivery traceability', 'Keep historic links', self.admin['id'])
        self.task = self.store.create_task(
            self.project_id, 'Implement workflow', self.requirement['id'],
            self.member['id'], '2026-10-20', self.admin['id'], 'todo')

    def test_requirement_delivery_archive_restore_and_reopen(self):
        self.store.require_permission(self.member, self.project_id, 'workitems.manage')
        requirement = self.store.update_requirement(self.project_id, self.requirement['id'], {
            'title': 'Verified delivery', 'version': self.requirement['version'],
        })
        task = self.task
        for status in ('doing', 'done'):
            with self.subTest(status=status):
                previous_version = task['version']
                task = self.store.update_task(self.project_id, task['id'], {
                    'status': status, 'version': previous_version,
                })
                self.assertEqual(task['version'], previous_version + 1)
                result = self.store.query_tasks(
                    self.project_id, assignee_user_id=self.member['id'],
                    requirement_id=requirement['id'], status=status, keyword='Verified')
                self.assertEqual(result['items'], [task])
                self.assertEqual(result['total'], 1)
        archived = self.store.archive_requirement(
            self.project_id, requirement['id'], requirement['version'])
        self.assertTrue(archived['archived'])
        self.assertEqual(self.store.list_requirements(self.project_id, include_archived=False), [])
        self.assertEqual(self.store.get_task(self.project_id, task['id']), task)
        with self.assertRaises(AppError) as denied:
            self.store.create_task(self.project_id, 'Blocked', requirement['id'],
                                   self.member['id'], None, self.admin['id'])
        self.assertEqual(denied.exception.status, 409)
        self.assertEqual(self.store.query_tasks(
            self.project_id, requirement_id=requirement['id'])['items'], [task])
        restored = self.store.restore_requirement(
            self.project_id, requirement['id'], archived['version'])
        new_task = self.store.create_task(self.project_id, 'Follow up', requirement['id'],
                                          self.member['id'], None, self.admin['id'])
        expected = self.store.list_tasks(self.project_id)
        self.store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.get_requirement(self.project_id, requirement['id']), restored)
        self.assertFalse(restored['archived'])
        self.assertEqual(self.store.list_tasks(self.project_id), expected)
        pages = [self.store.query_tasks(self.project_id, requirement_id=requirement['id'],
                                        page=page, page_size=1) for page in (1, 2)]
        self.assertEqual([page['items'][0] for page in pages], expected)
        self.assertEqual({row['id'] for row in expected}, {task['id'], new_task['id']})
        self.assertTrue(all(page['total'] == 2 and page['total_pages'] == 2 for page in pages))
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_member_removal_rejoin_does_not_revive_assignments(self):
        other = self.store.create_project('Unaffected project', creator=self.admin)
        self.store.add_member(other['id'], self.member['id'], self.role['id'], actor=self.admin)
        other_task = self.store.create_task(other['id'], 'Independent assignment', None,
                                            self.member['id'], None, self.admin['id'])
        self.store.remove_member(self.project_id, self.member['id'], actor=self.admin)
        with self.assertRaises(AppError) as denied:
            self.store.require_permission(self.member, self.project_id, 'project.view')
        self.assertEqual(denied.exception.status, 403)
        released = self.store.get_task(self.project_id, self.task['id'])
        self.assertIsNone(released['assignee_user_id'])
        self.assertIsNone(released['assignee_email'])
        self.assertEqual(released['version'], self.task['version'] + 1)
        self.assertEqual(released['requirement_id'], self.requirement['id'])
        self.assertEqual(self.store.query_tasks(
            self.project_id, assignee_user_id=self.member['id'])['total'], 0)
        self.assertEqual(self.store.query_tasks(self.project_id, unassigned=True)['items'], [released])
        self.assertEqual(self.store.get_task(other['id'], other_task['id']), other_task)
        self.store.require_permission(self.member, other['id'], 'workitems.manage')
        self.store.add_member(self.project_id, self.member['id'], self.role['id'], actor=self.admin)
        self.store.require_permission(self.member, self.project_id, 'workitems.manage')
        self.assertEqual(self.store.get_task(self.project_id, self.task['id']), released)
        with self.assertRaises(AppError) as stale:
            self.store.update_task(self.project_id, self.task['id'], {
                'assignee_user_id': self.member['id'], 'version': self.task['version'],
            })
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(self.store.get_task(self.project_id, self.task['id']), released)
        reassigned = self.store.update_task(self.project_id, self.task['id'], {
            'assignee_user_id': self.member['id'], 'version': released['version'],
        })
        self.assertEqual(self.store.query_tasks(
            self.project_id, assignee_user_id=self.member['id'])['items'], [reassigned])
        self.assertEqual(self.store.query_tasks(self.project_id, unassigned=True)['total'], 0)

    def test_two_connections_keep_requirement_and_task_versions_independent(self):
        other = Store(self.path)
        self.addCleanup(other.close)
        archived = other.archive_requirement(
            self.project_id, self.requirement['id'], self.requirement['version'])
        with self.assertRaises(AppError) as stale_requirement:
            self.store.restore_requirement(self.project_id, self.requirement['id'],
                                           self.requirement['version'])
        self.assertEqual(stale_requirement.exception.status, 409)
        self.assertEqual(self.store.get_requirement(self.project_id, self.requirement['id']), archived)
        moved = self.store.update_task(self.project_id, self.task['id'], {
            'status': 'doing', 'version': self.task['version'],
        })
        with self.assertRaises(AppError) as stale_task:
            other.update_task(self.project_id, self.task['id'], {
                'title': 'Stale edit', 'version': self.task['version'],
            })
        self.assertEqual(stale_task.exception.status, 409)
        self.assertEqual(other.get_task(self.project_id, self.task['id']), moved)
        self.assertEqual(other.get_requirement(self.project_id, self.requirement['id']), archived)
        restored = other.restore_requirement(self.project_id, self.requirement['id'], archived['version'])
        done = self.store.update_task(self.project_id, self.task['id'], {
            'status': 'done', 'version': moved['version'],
        })
        self.assertFalse(self.store.get_requirement(self.project_id, restored['id'])['archived'])
        self.assertEqual(other.query_tasks(self.project_id, status='doing')['total'], 0)
        self.assertEqual(other.query_tasks(self.project_id, status='done',
                                          requirement_id=restored['id'])['items'], [done])


if __name__ == '__main__':
    unittest.main(verbosity=2)
