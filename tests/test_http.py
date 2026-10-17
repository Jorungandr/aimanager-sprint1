"""直接请求 API，验证前端之外的越权阻断。"""
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
import socket
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server, SESSION_TTL, LOGIN_WINDOW, LOGIN_LIMIT


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.tmp.name) / 'api.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(HTTPCookieProcessor(CookieJar()))
        self.member = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close(); self.server.store.close()
        self.tmp.cleanup()

    def req(self, opener, path, method='GET', data=None):
        body = json.dumps(data or {}).encode() if method in ('POST', 'PUT') else None
        request = Request(self.base + path, data=body, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            with opener.open(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_member_cannot_edit_roles_or_members_and_loses_access_on_removal(self):
        self.assertEqual(self.req(self.admin, '/api/setup', 'POST',
                                  {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 201)
        _, user = self.req(self.admin, '/api/users', 'POST',
                           {'email': 'member@example.test', 'password': 'MemberPass123'})
        _, role = self.req(self.admin, '/api/roles', 'POST', {'name': '成员'})
        self.req(self.admin, f"/api/roles/{role['id']}/permissions", 'PUT', {'permissions': ['project.view']})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '蜂鸟'})
        self.req(self.admin, f"/api/projects/{project['id']}/members", 'POST',
                 {'user_id': user['id'], 'role_id': role['id']})
        self.req(self.member, '/api/login', 'POST',
                 {'email': 'member@example.test', 'password': 'MemberPass123'})
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members")[0], 200)
        self.assertEqual(self.req(self.member, '/api/roles')[0], 403)
        self.assertEqual(self.req(self.member, '/api/roles', 'POST', {'name': '伪管理员'})[0], 403)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/manage-check")[0], 403)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members", 'POST',
                                  {'user_id': user['id'], 'role_id': role['id']})[0], 403)
        self.assertEqual(self.req(self.admin, f"/api/projects/{project['id']}/members/{user['id']}", 'DELETE')[0], 200)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members")[0], 403)

    def test_granted_management_permissions_apply_on_server(self):
        self.assertEqual(self.req(self.admin, '/api/setup', 'POST',
                                  {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 201)
        _, manager = self.req(self.admin, '/api/users', 'POST',
                              {'email': 'manager@example.test', 'password': 'ManagerPass123'})
        _, role = self.req(self.admin, '/api/roles', 'POST', {'name': '项目经理'})
        self.req(self.admin, f"/api/roles/{role['id']}/permissions", 'PUT',
                 {'permissions': ['project.view', 'members.manage', 'projects.manage', 'roles.manage']})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '蜂鸟'})
        self.req(self.admin, f"/api/projects/{project['id']}/members", 'POST',
                 {'user_id': manager['id'], 'role_id': role['id']})
        self.req(self.member, '/api/login', 'POST',
                 {'email': 'manager@example.test', 'password': 'ManagerPass123'})
        self.assertEqual(self.req(self.member, '/api/roles')[0], 200)
        self.assertEqual(self.req(self.member, '/api/user-options')[0], 200)
        self.assertEqual(self.req(self.member, '/api/roles', 'POST', {'name': '观察者'})[0], 201)
        self.assertEqual(self.req(self.member, '/api/projects', 'POST', {'name': '新项目'})[0], 201)

    def test_sprint2_work_items_and_sprint1_regression(self):
        self.assertEqual(self.req(self.admin, '/api/setup', 'POST',
                                  {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 201)
        _, manager = self.req(self.admin, '/api/users', 'POST',
                              {'email': 'manager@example.test', 'password': 'ManagerPass123'})
        _, viewer = self.req(self.admin, '/api/users', 'POST',
                             {'email': 'viewer@example.test', 'password': 'ViewerPass123'})
        _, manager_role = self.req(self.admin, '/api/roles', 'POST', {'name': '项目经理'})
        self.req(self.admin, f"/api/roles/{manager_role['id']}/permissions", 'PUT',
                 {'permissions': ['project.view', 'members.manage', 'workitems.manage']})
        _, viewer_role = self.req(self.admin, '/api/roles', 'POST', {'name': '观察者'})
        self.req(self.admin, f"/api/roles/{viewer_role['id']}/permissions", 'PUT',
                 {'permissions': ['project.view']})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '蜂鸟'})
        self.req(self.admin, f"/api/projects/{project['id']}/members", 'POST',
                 {'user_id': manager['id'], 'role_id': manager_role['id']})
        self.req(self.admin, f"/api/projects/{project['id']}/members", 'POST',
                 {'user_id': viewer['id'], 'role_id': viewer_role['id']})
        self.req(self.member, '/api/login', 'POST',
                 {'email': 'manager@example.test', 'password': 'ManagerPass123'})
        status, requirement = self.req(self.member, f"/api/projects/{project['id']}/requirements", 'POST',
                                       {'title': '看板管理', 'description': '推进任务状态'})
        self.assertEqual(status, 201)
        status, task = self.req(self.member, f"/api/projects/{project['id']}/tasks", 'POST', {
            'title': '实现任务卡片', 'requirement_id': requirement['id'],
            'assignee_user_id': viewer['id'], 'due_date': '2026-10-20', 'status': 'todo'})
        self.assertEqual(status, 201)
        status, moved = self.req(self.member,
                                 f"/api/projects/{project['id']}/tasks/{task['id']}", 'PUT',
                                 {'status': 'doing', 'version': task['version']})
        self.assertEqual((status, moved['status']), (200, 'doing'))
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members")[0], 200)
        viewer_client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.req(viewer_client, '/api/login', 'POST',
                 {'email': 'viewer@example.test', 'password': 'ViewerPass123'})
        self.assertEqual(self.req(viewer_client, f"/api/projects/{project['id']}/tasks")[0], 200)
        self.assertEqual(self.req(viewer_client, f"/api/projects/{project['id']}/tasks", 'POST',
                                  {'title': '越权任务'})[0], 403)
        task_path = f"/api/projects/{project['id']}/tasks/{task['id']}"
        self.assertEqual(self.req(viewer_client, task_path, 'PUT', {'status': 'done'})[0], 403)
        self.req(self.admin, f"/api/projects/{project['id']}/members/{manager['id']}", 'DELETE')
        self.assertEqual(self.req(self.member, task_path, 'PUT', {'status': 'done'})[0], 403)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/tasks")[0], 403)

    def test_api_assignment_ceiling_and_creator_access(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        _, user = self.req(self.admin, '/api/users', 'POST', {'email': 'user@example.test', 'password': 'MemberPass123'})
        _, low = self.req(self.admin, '/api/roles', 'POST', {'name': '成员管理'})
        _, high = self.req(self.admin, '/api/roles', 'POST', {'name': '平台管理'})
        self.req(self.admin, f"/api/roles/{low['id']}/permissions", 'PUT', {'permissions': ['members.manage', 'projects.manage']})
        self.req(self.admin, f"/api/roles/{high['id']}/permissions", 'PUT', {'permissions': ['roles.manage', 'projects.manage']})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '来源项目'})
        self.req(self.admin, f"/api/projects/{project['id']}/members", 'POST', {'user_id': user['id'], 'role_id': low['id']})
        self.req(self.member, '/api/login', 'POST', {'email': 'user@example.test', 'password': 'MemberPass123'})
        status, choices = self.req(self.member, f"/api/projects/{project['id']}/role-options")
        self.assertEqual(status, 200)
        self.assertNotIn(high['id'], [item['id'] for item in choices])
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members", 'POST',
                                  {'user_id': user['id'], 'role_id': high['id']})[0], 403)
        status, created = self.req(self.member, '/api/projects', 'POST', {'name': '自己创建'})
        self.assertEqual(status, 201)
        self.assertIn(created, self.req(self.member, '/api/projects')[1])
        self.assertEqual(self.req(self.member, f"/api/projects/{created['id']}/requirements", 'POST', {'title': '需求'})[0], 201)
        self.assertEqual(self.req(self.member, f"/api/projects/{created['id']}/members/{user['id']}", 'DELETE')[0], 409)

    def test_version_conflict_and_input_validation_via_api(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': '项目'})
        base = f"/api/projects/{project['id']}/tasks"
        self.assertEqual(self.req(self.admin, base, 'POST', {'title': {'bad': 1}})[0], 400)
        _, task = self.req(self.admin, base, 'POST', {'title': '任务'})
        path = f"{base}/{task['id']}"
        status, first = self.req(self.admin, path, 'PUT', {'title': '新标题', 'version': task['version']})
        self.assertEqual(status, 200)
        self.assertEqual(self.req(self.admin, path, 'PUT', {'status': 'done', 'version': task['version']})[0], 409)
        self.assertEqual(self.req(self.admin, path, 'PUT', {'status': 'done'})[0], 428)
        self.assertEqual(self.req(self.admin, base)[1][0], first)

    def test_session_expiry_and_logout(self):
        now = [1000.0]
        self.server.clock = lambda: now[0]
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        self.assertEqual(self.req(self.admin, '/api/me')[0], 200)
        now[0] += SESSION_TTL
        self.assertEqual(self.req(self.admin, '/api/me')[0], 401)
        self.req(self.admin, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        self.req(self.admin, '/api/logout', 'POST')
        self.assertEqual(self.req(self.admin, '/api/me')[0], 401)

    def test_login_rate_limit_recovers_after_window(self):
        now = [1000.0]
        self.server.clock = lambda: now[0]
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        for _ in range(LOGIN_LIMIT):
            self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'wrong'})[0], 401)
        self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 429)
        now[0] += LOGIN_WINDOW + 1
        self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 200)

    def test_unexpected_database_error_is_json_500(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        with patch.object(self.server.store, 'list_projects', side_effect=RuntimeError('internal secret')), self.assertLogs(level='ERROR'):
            status, body = self.req(self.admin, '/api/projects')
        self.assertEqual(status, 500)
        self.assertNotIn('secret', body['error'])

    def test_unauthenticated_api_and_server_isolated_sessions(self):
        self.assertEqual(self.req(self.member, '/api/projects')[0], 401)
        self.assertEqual(self.req(self.member, '/api/projects/1/tasks', 'POST', {'title': '拒绝'})[0], 401)
        self.assertEqual(self.server.sessions, {})

    def test_slow_request_body_does_not_block_other_clients(self):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as slow:
            slow.sendall(f'POST /api/login HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\nContent-Length: 100\r\n\r\n'.encode())
            request = Request(self.base + '/api/status')
            with self.member.open(request, timeout=2) as response:
                self.assertEqual(response.status, 200)

    def test_raw_malformed_json_is_rejected(self):
        request = Request(self.base + '/api/login', data=b'{broken',
                          headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=2)
        with failure.exception as response:
            self.assertEqual(response.status, 400)
            self.assertIn('error', json.load(response))

    def test_cross_origin_write_is_rejected_before_setup(self):
        request = Request(self.base + '/api/setup', data=b'{}', headers={'Origin': 'https://untrusted.example'})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=2)
        with failure.exception as response:
            self.assertEqual(response.status, 403)
        self.assertTrue(self.server.store.needs_setup())


if __name__ == '__main__': unittest.main()
