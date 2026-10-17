"""Task filtering and pagination regression, using only isolated SQLite databases."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import AppError, Store


class TaskQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')
        self.project = self.store.create_project('Project', creator=self.admin)
        self.other = self.store.create_project('Other')
        self.requirement = self.store.create_requirement(self.project['id'], '追溯需求', '', self.admin['id'])
        self.tasks = []
        for title, status, assignee, requirement in [
            ('Alpha 100%_done', 'todo', None, self.requirement['id']),
            ('Beta', 'doing', self.admin['id'], self.requirement['id']),
            ('Gamma', 'done', None, None),
            ('Delta', 'backlog', None, None),
            ('Alpha second', 'todo', None, None),
        ]:
            self.tasks.append(self.store.create_task(self.project['id'], title, requirement,
                                                     assignee, None, self.admin['id'], status))
        self.store.create_task(self.other['id'], 'Alpha hidden', None, None, None, self.admin['id'])

    def tearDown(self):
        self.store.close()

    def query(self, **filters):
        return self.store.query_tasks(self.project['id'], **filters)

    def test_default_matches_legacy_list_and_excludes_other_project(self):
        result = self.query()
        self.assertEqual(result['items'], self.store.list_tasks(self.project['id']))
        self.assertEqual((result['total'], result['page'], result['page_size'], result['total_pages']), (5, 1, 20, 1))

    def test_combined_filters(self):
        result = self.query(assignee_user_id=self.admin['id'], status='doing', requirement_id=self.requirement['id'])
        self.assertEqual([task['title'] for task in result['items']], ['Beta'])
        self.assertEqual(result['total'], 1)

    def test_unassigned_is_distinct_from_all(self):
        self.assertEqual(self.query()['total'], 5)
        self.assertEqual(self.query(unassigned=True)['total'], 4)
        self.assertEqual(self.query(assignee_user_id=9999)['total'], 0)
        with self.assertRaises(AppError):
            self.query(unassigned=True, assignee_user_id=self.admin['id'])

    def test_literal_search_and_requirement_title(self):
        self.assertEqual(self.query(keyword='alpha')['total'], 2)
        self.assertEqual(self.query(keyword='%_')['total'], 1)
        self.assertEqual(self.query(keyword="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.query(keyword='追溯')['total'], 2)

    def test_pages_keep_tie_order_without_duplicates(self):
        with self.store.db:
            self.store.db.execute('UPDATE tasks SET position=0 WHERE project_id=?', (self.project['id'],))
        expected = self.store.list_tasks(self.project['id'])
        received = []
        for page in range(1, 4):
            result = self.query(page=page, page_size=2)
            self.assertEqual((result['total'], result['total_pages']), (5, 3))
            received.extend(result['items'])
        self.assertEqual(received, expected)
        self.assertEqual(len({row['id'] for row in received}), 5)

    def test_empty_results_and_out_of_range_page(self):
        self.assertEqual(self.query(keyword='absent')['total_pages'], 0)
        far = self.query(page=100, page_size=2)
        self.assertEqual(far['items'], [])
        self.assertEqual((far['total'], far['total_pages']), (5, 3))

    def test_invalid_parameters(self):
        for filters in ({'page': 0}, {'page': True}, {'page': 1.5}, {'page': 1_000_001},
                        {'page_size': 101}, {'page_size': -1}, {'status': 'closed'},
                        {'keyword': None}, {'keyword': 'x' * 101}, {'unassigned': 'true'},
                        {'assignee_user_id': False}, {'requirement_id': 0}):
            with self.subTest(filters=filters), self.assertRaises(AppError):
                self.query(**filters)

    def test_cross_project_requirement_rejected_and_archived_history_kept(self):
        other_requirement = self.store.create_requirement(self.other['id'], 'Other', '', self.admin['id'])
        with self.assertRaises(AppError) as denied:
            self.query(requirement_id=other_requirement['id'])
        self.assertEqual(denied.exception.status, 404)
        self.store.archive_requirement(self.project['id'], self.requirement['id'], 1)
        self.assertEqual(self.query(requirement_id=self.requirement['id'])['total'], 2)

    def test_query_does_not_commit_outer_transaction(self):
        self.store.db.execute('BEGIN')
        self.store.db.execute("UPDATE tasks SET title='Uncommitted' WHERE id=?", (self.tasks[0]['id'],))
        self.assertEqual(self.query(keyword='Uncommitted')['total'], 1)
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertEqual(self.query(keyword='Uncommitted')['total'], 0)


if __name__ == '__main__':
    unittest.main()
