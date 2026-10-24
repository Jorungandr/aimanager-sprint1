"""Sprint 3 接口回归：统计、风险、下钻与变更记录的权限和参数边界。"""
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server


class StatsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.tmp.name) / 'stats-api.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.member = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.outsider = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.prepare()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.store.close()
        self.tmp.cleanup()

    def req(self, opener, path, method='GET', data=None):
        body = json.dumps(data or {}).encode() if method in ('POST', 'PUT') else None
        request = Request(self.base + path, data=body, method=method,
                          headers={'Content-Type': 'application/json'})
        if method in ('POST', 'PUT', 'DELETE'):
            request.add_header('X-CSRF-Token', self.csrf_token(opener))
        try:
            with opener.open(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def csrf_token(self, opener):
        with opener.open(self.base + '/api/csrf', timeout=3) as response:
            return json.load(response)['csrf_token']

    def prepare(self):
        self.req(self.admin, '/api/setup', 'POST',
                 {'email': 'admin@example.test', 'password': 'AdminPass123'})
        _, user = self.req(self.admin, '/api/users', 'POST',
                           {'email': 'member@example.test', 'password': 'MemberPass123'})
        _, outsider = self.req(self.admin, '/api/users', 'POST',
                               {'email': 'outsider@example.test', 'password': 'OutsiderPass123'})
        _, role = self.req(self.admin, '/api/roles', 'POST', {'name': '成员'})
        self.req(self.admin, f"/api/roles/{role['id']}/permissions", 'PUT',
                 {'permissions': ['project.view']})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '凤凰'})
        self.project_id = project['id']
        self.req(self.admin, f'/api/projects/{project["id"]}/members', 'POST',
                 {'user_id': user['id'], 'role_id': role['id']})
        self.req(self.member, '/api/login', 'POST',
                 {'email': 'member@example.test', 'password': 'MemberPass123'})
        self.req(self.outsider, '/api/login', 'POST',
                 {'email': 'outsider@example.test', 'password': 'OutsiderPass123'})
        _, requirement = self.req(self.admin, f'/api/projects/{project["id"]}/requirements',
                                  'POST', {'title': '需求A', 'description': ''})
        self.requirement_id = requirement['id']
        self.task_ids = []
        for title, status, due in [('Done', 'done', None), ('Doing', 'doing', '2020-01-01'),
                                   ('Todo', 'todo', None)]:
            _, task = self.req(self.admin, f'/api/projects/{project["id"]}/tasks', 'POST',
                               {'title': title, 'requirement_id': requirement['id'],
                                'due_date': due, 'status': status})
            self.task_ids.append(task['id'])

    def test_stats_visible_to_project_members_only(self):
        status, stats = self.req(self.member, f'/api/projects/{self.project_id}/stats')
        self.assertEqual(status, 200)
        self.assertEqual((stats['total'], stats['completed'], stats['overdue']), (3, 1, 1))
        self.assertEqual(len(stats['trend']), 14)
        self.assertEqual(self.req(self.outsider, f'/api/projects/{self.project_id}/stats')[0], 403)
        self.assertEqual(self.req(self.outsider, f'/api/projects/{self.project_id}/risks')[0], 403)
        self.assertEqual(self.req(self.outsider, f'/api/projects/{self.project_id}/audit')[0], 403)

    def test_drilldown_requires_known_dimension(self):
        base = f'/api/projects/{self.project_id}/stats/tasks'
        status, data = self.req(self.member, base + '?dimension=status&value=done')
        self.assertEqual((status, data['total']), (200, 1))
        self.assertEqual(data['items'][0]['title'], 'Done')
        self.assertEqual(self.req(self.member, base + '?value=done')[0], 400)
        self.assertEqual(self.req(self.member, base + '?dimension=owner&value=1')[0], 400)
        self.assertEqual(self.req(self.member, base + '?dimension=status&value=done&extra=1')[0], 400)
        self.assertEqual(self.req(self.member, base + '?dimension=status&value=done&value=todo')[0], 400)
        self.assertEqual(self.req(self.member, base + '?dimension=status&value=done&limit=201')[0], 400)

    def test_audit_lists_actor_and_rejects_bad_filters(self):
        base = f'/api/projects/{self.project_id}/audit'
        status, events = self.req(self.member, base)
        self.assertEqual(status, 200)
        self.assertTrue(events)
        self.assertEqual(events[0]['actor_email'], 'admin@example.test')
        self.assertEqual({event['entity_type'] for event in events}, {'task', 'requirement', 'membership'})
        self.assertEqual(self.req(self.member, base + '?entity_type=task&limit=2')[0], 200)
        self.assertEqual(self.req(self.member, base + '?entity_type=project')[0], 400)
        self.assertEqual(self.req(self.member, base + '?limit=101')[0], 400)

    def test_stats_rejects_unknown_query_and_method(self):
        self.assertEqual(self.req(self.member, f'/api/projects/{self.project_id}/stats?days=91')[0], 400)
        self.assertEqual(self.req(self.member, f'/api/projects/{self.project_id}/stats?window=3')[0], 400)
        self.assertEqual(self.req(self.member, f'/api/projects/{self.project_id}/stats', 'PUT', {})[0], 405)
        self.assertEqual(self.req(self.member, f'/api/projects/{self.project_id}/risks?limit=51')[0], 400)

    def test_task_update_records_actor_and_from_to(self):
        task_id = self.task_ids[2]
        status, task = self.req(self.admin, f'/api/projects/{self.project_id}/tasks')
        current = next(item for item in task if item['id'] == task_id)
        self.assertEqual(self.req(self.admin, f'/api/projects/{self.project_id}/tasks/{task_id}', 'PUT',
                                  {'status': 'doing', 'version': current['version']})[0], 200)
        _, events = self.req(self.admin, f'/api/projects/{self.project_id}/audit'
                             f'?entity_type=task&entity_id={task_id}')
        status_event = next(event for event in events if event['field'] == 'status')
        self.assertEqual((status_event['from_value'], status_event['to_value']), ('todo', 'doing'))
        self.assertEqual(status_event['actor_email'], 'admin@example.test')
        # 换列同时会把卡片排到目标列末尾，位置变化同样留痕
        self.assertIn('position', {event['field'] for event in events})

    def test_read_only_member_cannot_change_tasks(self):
        status, task = self.req(self.member, f'/api/projects/{self.project_id}/tasks')
        current = next(item for item in task if item['id'] == self.task_ids[2])
        self.assertEqual(self.req(self.member, f'/api/projects/{self.project_id}/tasks/{current["id"]}',
                                  'PUT', {'status': 'doing', 'version': current['version']})[0], 403)


if __name__ == '__main__':
    unittest.main()
