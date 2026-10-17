"""HTTP integration contract, also loaded by test_business_api_contract.py.

Run: python tests/contracts/business_api_contract.py -v
Missing routes must FAIL, not skip or pass as expected failures.
No production database is opened. Run against the personal business branch;
the routes must delegate to its versioned requirement and task operations.
"""
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import create_server


class BusinessApiContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.server = create_server(str(Path(temporary.name) / 'contract.sqlite'), 0)
        self.addCleanup(self.server.store.close)
        self.addCleanup(self.server.server_close)
        store = self.server.store
        self.owner = store.setup('owner@example.test', 'OwnerPass123')
        viewer = store.create_user('viewer@example.test', 'ViewerPass123')
        role = store.create_role('Read only')
        store.set_permissions(role['id'], ['project.view'], actor=self.owner)
        project = store.create_project('Contract project', creator=self.owner)
        other = store.create_project('Other project', creator=self.owner)
        store.add_member(project['id'], viewer['id'], role['id'], actor=self.owner)
        self.project_id = project['id']
        self.base_path = f"/api/projects/{project['id']}"
        self.other_path = f"/api/projects/{other['id']}"
        self.requirement = store.create_requirement(project['id'], 'Contract requirement', '', self.owner['id'])
        self.requirement_path = f"{self.base_path}/requirements/{self.requirement['id']}"
        self.task = store.create_task(project['id'], 'Contract task', self.requirement['id'],
                                      viewer['id'], None, self.owner['id'], 'todo')
        self.task_path = f"{self.base_path}/tasks/{self.task['id']}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(HTTPCookieProcessor(CookieJar()))
        self.viewer = build_opener(HTTPCookieProcessor(CookieJar()))
        self.anonymous = build_opener(HTTPCookieProcessor(CookieJar()))
        for client, email, password in (
            (self.admin, 'owner@example.test', 'OwnerPass123'),
            (self.viewer, 'viewer@example.test', 'ViewerPass123'),
        ):
            self.expect(client, '/api/login', 200, 'POST', {'email': email, 'password': password})

    def expect(self, client, path, status, method='GET', data=None):
        body = json.dumps(data if data is not None else {}).encode('utf-8') if method in ('POST', 'PUT') else None
        request = Request(self.base_url + path, data=body, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            response = client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.headers.get_content_type(), 'application/json')
            result = json.load(response)
            self.assertEqual(response.code, status, f'{method} {path}: {result}')
        if status >= 400:
            self.assertIsInstance(result, dict)
            self.assertIsInstance(result.get('error'), str)
            self.assertTrue(result['error'])
        return result

    def snapshot(self):
        with self.server.store_lock:
            return list(self.server.store.db.iterdump())

    def test_legacy_task_list_and_authenticated_creator_remain_compatible(self):
        tasks = self.expect(self.viewer, self.base_path + '/tasks', 200)
        self.assertIsInstance(tasks, list)
        self.assertEqual(tasks, [self.task])
        created = self.expect(self.admin, self.base_path + '/requirements', 201, 'POST', {
            'title': 'Authenticated creator', 'created_by': self.task['assignee_user_id'],
        })
        self.assertEqual(created['created_by'], self.owner['id'])
        self.assertIs(type(created['version']), int)
        self.assertEqual(created['version'], 1)
        self.assertIs(created['archived'], False)

    def test_anonymous_requests_are_rejected_before_access(self):
        before = self.snapshot()
        for path, method, data in (
            (self.requirement_path, 'GET', None),
            (self.requirement_path, 'PUT', {'title': 'Denied', 'version': 1}),
            (self.requirement_path + '/archive', 'POST', {'version': 1}),
            (self.requirement_path + '/restore', 'POST', {'version': 1}),
            (self.base_path + '/tasks/query', 'GET', None),
        ):
            with self.subTest(path=path, method=method):
                self.expect(self.anonymous, path, 401, method, data)
        self.assertEqual(self.snapshot(), before)

    def test_viewer_reads_requirement_but_cannot_modify(self):
        before = self.snapshot()
        requirement = self.expect(self.viewer, self.requirement_path, 200)
        self.assertEqual(requirement, self.requirement)
        for path, method, data in (
            (self.requirement_path, 'PUT', {'title': 'Denied', 'version': 1}),
            (self.requirement_path + '/archive', 'POST', {'version': 1}),
            (self.requirement_path + '/restore', 'POST', {'version': 1}),
        ):
            with self.subTest(path=path):
                self.expect(self.viewer, path, 403, method, data)
        self.assertEqual(self.snapshot(), before)

    def test_requirement_edit_requires_version_and_preserves_original_on_failure(self):
        updated = self.expect(self.admin, self.requirement_path, 200, 'PUT', {
            'title': 'Updated requirement', 'description': 'Edited', 'version': 1,
        })
        self.assertEqual((updated['title'], updated['description'], updated['version']),
                         ('Updated requirement', 'Edited', 2))
        self.assertEqual(updated['created_by'], self.requirement['created_by'])
        self.assertEqual(updated['created_at'], self.requirement['created_at'])
        before = self.snapshot()
        for payload, status in (
            ({'title': 'Missing version'}, 428),
            ({'title': 'Stale version', 'version': 1}, 409),
            ({'title': '', 'version': 2}, 400),
            ({'description': None, 'version': 2}, 400),
            ({'archived': True, 'version': 2}, 400),
        ):
            with self.subTest(payload=payload):
                self.expect(self.admin, self.requirement_path, status, 'PUT', payload)
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.expect(self.admin, self.requirement_path, 200), updated)
        self.expect(self.admin, self.requirement_path, 405, 'DELETE')
        self.assertEqual(self.snapshot(), before)

    def test_archive_restore_preserves_task_links_and_checks_versions(self):
        before = self.snapshot()
        self.expect(self.admin, self.requirement_path + '/archive', 428, 'POST', {})
        self.assertEqual(self.snapshot(), before)
        archived = self.expect(self.admin, self.requirement_path + '/archive', 200, 'POST', {'version': 1})
        self.assertIs(archived['archived'], True)
        self.assertEqual(archived['version'], 2)
        self.assertEqual(self.expect(self.admin, self.requirement_path + '/archive',
                                     200, 'POST', {'version': 2}), archived)
        before = self.snapshot()
        self.expect(self.admin, self.requirement_path + '/restore', 409, 'POST', {'version': 1})
        self.expect(self.admin, self.requirement_path, 409, 'PUT', {'title': 'Blocked', 'version': 2})
        self.expect(self.admin, self.base_path + '/tasks', 409, 'POST', {
            'title': 'Blocked association', 'requirement_id': self.requirement['id'],
        })
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.expect(self.admin, self.base_path + '/tasks', 200), [self.task])
        moved = self.expect(self.admin, self.task_path, 200, 'PUT', {'status': 'done', 'version': 1})
        self.assertEqual(moved['requirement_id'], self.requirement['id'])
        restored = self.expect(self.admin, self.requirement_path + '/restore', 200, 'POST', {'version': 2})
        self.assertIs(restored['archived'], False)
        self.assertEqual(restored['version'], 3)
        created = self.expect(self.admin, self.base_path + '/tasks', 201, 'POST', {
            'title': 'Allowed association', 'requirement_id': self.requirement['id'],
        })
        self.assertEqual(created['requirement_id'], self.requirement['id'])

    def test_cross_project_requirement_is_not_found_without_mutation(self):
        # A known-good route prevents an unimplemented route from passing as 404.
        self.expect(self.admin, self.requirement_path, 200)
        before = self.snapshot()
        path = f"{self.other_path}/requirements/{self.requirement['id']}"
        for route, method, data in (
            (path, 'GET', None), (path, 'PUT', {'title': 'Wrong project', 'version': 1}),
            (path + '/archive', 'POST', {'version': 1}),
            (path + '/restore', 'POST', {'version': 1}),
        ):
            with self.subTest(path=route, method=method):
                self.expect(self.admin, route, 404, method, data)
        self.expect(self.viewer, self.other_path + '/tasks/query', 403)
        self.assertEqual(self.snapshot(), before)

    def test_query_rejects_ambiguous_and_invalid_parameters_without_writes(self):
        before = self.snapshot()
        queries = (
            'page=', 'page_size=', 'status=', 'assignee_user_id=', 'requirement_id=',
            'page=1&page=2', 'keyword=a&keyword=b', 'unknown=1', 'pageSize=2',
            'page=-1', 'page=1.5', 'page=1000001', 'page_size=0',
            'assignee_user_id=false', 'assignee_user_id=9223372036854775808',
            'requirement_id=0', 'unassigned=', 'keyword=' + 'x' * 101,
        )
        for query in queries:
            with self.subTest(query=query):
                self.expect(self.viewer, self.base_path + '/tasks/query?' + query, 400)
                self.assertEqual(self.snapshot(), before)

    def test_query_combines_filters_and_empty_optional_search(self):
        created = self.expect(self.admin, self.base_path + '/tasks', 201, 'POST', {
            'title': 'Unassigned extra', 'status': 'doing',
        })
        before = self.snapshot()
        result = self.expect(self.viewer, self.base_path + '/tasks/query?keyword=&unassigned=false', 200)
        self.assertEqual(result['total'], 2)
        filtered = self.expect(self.viewer, self.base_path + '/tasks/query?' + urlencode({
            'unassigned': 'true', 'status': 'doing', 'keyword': 'extra', 'page_size': 1,
        }), 200)
        self.assertEqual(filtered['items'], [created])
        empty = self.expect(self.viewer, self.base_path + '/tasks/query?status=done', 200)
        self.assertEqual((empty['items'], empty['total'], empty['total_pages']), ([], 0, 0))
        self.assertEqual(self.snapshot(), before)

    def test_failed_task_updates_preserve_entire_database(self):
        before = self.snapshot()
        for client, payload, status in (
            (self.viewer, {'status': 'done', 'version': 1}, 403),
            (self.admin, {'status': 'done'}, 428),
            (self.admin, {'status': 'done', 'version': 2}, 409),
            (self.admin, {'status': 'invalid', 'version': 1}, 400),
            (self.admin, {'due_date': '2026-02-30', 'version': 1}, 400),
            (self.admin, {'title': 'Changed', 'assignee_user_id': 999999, 'version': 1}, 400),
        ):
            with self.subTest(payload=payload, status=status):
                self.expect(client, self.task_path, status, 'PUT', payload)
                self.assertEqual(self.snapshot(), before)

    def test_query_contract_uses_filters_and_keeps_legacy_array(self):
        params = urlencode({'assignee_user_id': self.task['assignee_user_id'], 'status': 'todo',
                            'requirement_id': self.requirement['id'], 'keyword': 'Contract',
                            'page': 1, 'page_size': 1})
        result = self.expect(self.viewer, self.base_path + '/tasks/query?' + params, 200)
        self.assertEqual(set(result), {'items', 'total', 'page', 'page_size', 'total_pages'})
        self.assertEqual(result['items'], [self.task])
        for field in ('total', 'page', 'page_size', 'total_pages'):
            self.assertIs(type(result[field]), int)
            self.assertEqual(result[field], 1)
        empty = self.expect(self.viewer, self.base_path + '/tasks/query?unassigned=true', 200)
        self.assertEqual((empty['items'], empty['total'], empty['total_pages']), ([], 0, 0))
        beyond = self.expect(self.viewer, self.base_path + '/tasks/query?page=2&page_size=1', 200)
        self.assertEqual((beyond['items'], beyond['total'], beyond['page']), ([], 1, 2))
        before = self.snapshot()
        for query in ('page=0', 'page_size=101', 'status=closed', 'unassigned=invalid',
                      f"unassigned=true&assignee_user_id={self.task['assignee_user_id']}"):
            with self.subTest(query=query):
                self.expect(self.viewer, self.base_path + '/tasks/query?' + query, 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.expect(self.viewer, self.base_path + '/tasks', 200), [self.task])


if __name__ == '__main__':
    unittest.main(verbosity=2)
