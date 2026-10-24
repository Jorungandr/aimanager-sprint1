"""直接请求 API，验证前端之外的越权阻断。"""
from http.cookiejar import CookieJar
from email.message import Message
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
import socket
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server, Handler, LocalServer, SESSION_TTL, SESSION_LIMIT, LOGIN_WINDOW, LOGIN_LIMIT, LOGIN_IP_LIMIT, REJECT_DRAIN_LIMIT, REJECT_DRAIN_TIMEOUT, sanitize_log
from service import AppError


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.tmp.name) / 'api.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.member = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close(); self.server.store.close()
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

    def test_invalid_unicode_and_overflowing_json_numbers_never_write(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        token = self.csrf_token(self.admin)
        for body in [b'{"name":"\\ud800"}', b'{"name":"\\udfff"}',
                     b'{"name":"Bad","extra":{"\\ud800":1}}',
                     b'{"name":"Bad","extra":[1e999]}']:
            with self.subTest(body=body):
                request = Request(self.base + '/api/roles', data=body, headers={
                    'Content-Type': 'application/json', 'X-CSRF-Token': token,
                })
                with self.assertRaises(HTTPError) as failure:
                    self.admin.open(request, timeout=3)
                with failure.exception as response:
                    self.assertEqual(response.status, 400)
                    self.assertIn('error', json.load(response))
        self.assertEqual(self.req(self.admin, '/api/roles'), (200, []))

    def test_database_startup_failure_releases_listening_socket(self):
        created = []

        def allocate(*args, **kwargs):
            server = LocalServer(*args, **kwargs)
            created.append(server)
            return server

        try:
            with patch('server.LocalServer', side_effect=allocate), patch('server.Store', side_effect=OSError('Unavailable database')):
                with self.assertRaisesRegex(OSError, 'Unavailable database'):
                    create_server(':memory:', 0)
            self.assertEqual(created[0].socket.fileno(), -1)
        finally:
            for server in created:
                server.server_close()

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
        viewer_client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
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
        token = self.csrf_token(self.member)
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as slow:
            slow.sendall(f'POST /api/login HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\nCookie: csrf={token}\r\nX-CSRF-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n'.encode())
            request = Request(self.base + '/api/status')
            with self.member.open(request, timeout=2) as response:
                self.assertEqual(response.status, 200)

    def test_raw_malformed_json_is_rejected(self):
        request = Request(self.base + '/api/login', data=b'{broken',
                          headers={'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf_token(self.member)})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=2)
        with failure.exception as response:
            self.assertEqual(response.status, 400)
            self.assertIn('error', json.load(response))

    def test_ambiguous_and_nonstandard_json_is_rejected_without_writes(self):
        token = self.csrf_token(self.member)
        for body in [
            b'{"email":"first","email":"second"}',
            b'{"nested":{"name":1,"name":2}}',
            b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
            b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}',
        ]:
            with self.subTest(body=body[:80]):
                request = Request(self.base + '/api/setup', data=body, headers={
                    'Content-Type': 'application/json', 'X-CSRF-Token': token,
                })
                with self.assertRaises(HTTPError) as failure:
                    self.member.open(request, timeout=3)
                with failure.exception as response:
                    self.assertEqual(response.status, 400)
                    self.assertIn('error', json.load(response))
                self.assertTrue(self.server.store.needs_setup())

    def test_ambiguous_http_framing_is_rejected_before_body_read(self):
        token = self.csrf_token(self.member)
        for framing in [
            'Content-Length: 2\r\nContent-Length: 2',
            'Content-Length: 2\r\nContent-Length: 100',
            'Content-Length: +2', 'Content-Length: 2, 2',
            'Transfer-Encoding: chunked',
            'Transfer-Encoding: chunked\r\nContent-Length: 2',
        ]:
            with self.subTest(framing=framing):
                with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as client:
                    client.sendall((f'POST /api/setup HTTP/1.0\r\n'
                                    f'Host: 127.0.0.1:{self.server.server_port}\r\n'
                                    f'Cookie: csrf={token}\r\nX-CSRF-Token: {token}\r\n'
                                    f'Content-Type: application/json\r\n{framing}\r\n\r\n').encode())
                    response = client.makefile('rb')
                    with response:
                        self.assertIn(b' 400 ', response.readline())
                self.assertTrue(self.server.store.needs_setup())

    def test_successful_login_clears_failures_and_accounts_are_isolated(self):
        self.server.clock = lambda: 1000.0
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        login = lambda email, password: self.req(self.member, '/api/login', 'POST', {'email': email, 'password': password})[0]
        for _ in range(4):
            self.assertEqual(login(' ADMIN@example.test ', 'wrong'), 401)
        self.assertEqual(login('admin@example.test', 'AdminPass123'), 200)
        self.assertEqual(login('admin@example.test', 'wrong'), 401)
        self.assertEqual(login('admin@example.test', 'AdminPass123'), 200)
        self.assertEqual(self.server.login_failures, {})
        for _ in range(LOGIN_LIMIT):
            self.assertEqual(login('missing@example.test', 'wrong'), 401)
        self.assertEqual(login('admin@example.test', 'AdminPass123'), 200)
        self.assertEqual(login('missing@example.test', 'wrong'), 429)

    def test_login_backoff_and_expired_key_cleanup(self):
        now = [1000.0]
        self.server.clock = lambda: now[0]
        for _ in range(LOGIN_LIMIT):
            self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'missing@example.test'})[0], 401)
        for delay in [1, 2, 4]:
            self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'missing@example.test'})[0], 429)
            now[0] += delay
            self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'missing@example.test'})[0], 401)
        now[0] += LOGIN_WINDOW + 1
        self.req(self.member, '/api/login', 'POST', {'email': 'another@example.test'})
        self.assertEqual(set(self.server.login_failures), {('another@example.test', '127.0.0.1')})

    def test_login_backoff_starts_when_authentication_finishes(self):
        now = [1000.0]
        self.server.clock = lambda: now[0]

        def slow_failure(*args):
            now[0] += 2
            raise AppError('Invalid credentials', 401)

        with patch.object(self.server.store, 'authenticate', side_effect=slow_failure):
            for _ in range(LOGIN_LIMIT):
                self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'missing@example.test'})[0], 401)
            self.assertEqual(self.req(self.member, '/api/login', 'POST', {'email': 'missing@example.test'})[0], 429)
        self.assertEqual(self.server.login_failures[('missing@example.test', '127.0.0.1')][-1], now[0])

    def test_sessions_are_hashed_rotated_and_bounded(self):
        _, user = self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        clients = []
        with patch.object(self.server.store, 'authenticate', return_value=user):
            for _ in range(SESSION_LIMIT + 1):
                client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
                self.assertEqual(self.req(client, '/api/login', 'POST')[0], 200)
                clients.append(client)
            self.assertEqual(self.req(clients[0], '/api/me')[0], 401)
            for _ in range(400):
                self.assertEqual(self.req(clients[-1], '/api/login', 'POST')[0], 200)
        self.assertEqual(len(self.server.sessions), SESSION_LIMIT)
        self.assertTrue(all(len(key) == 64 for key in self.server.sessions))
        self.req(clients[-1], '/api/logout', 'POST')
        self.assertEqual(self.req(clients[-1], '/api/me')[0], 401)
        self.assertEqual(len(self.server.sessions), SESSION_LIMIT - 1)

    def test_missing_projects_return_404_to_admin(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        for resource in ['tasks', 'members', 'requirements', 'tasks/query', 'manage-check']:
            self.assertEqual(self.req(self.admin, f'/api/projects/999/{resource}')[0], 404)

    def test_restart_invalidates_sessions_without_losing_accounts(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server.store.close()
        self.server = create_server(str(Path(self.tmp.name) / 'api.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.req(self.admin, '/api/me')[0], 401)
        self.assertEqual(self.req(self.admin, '/api/status'), (200, {'needs_setup': False}))
        self.assertEqual(self.req(self.admin, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 200)

    def test_unassigned_account_has_no_role_options(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        self.req(self.admin, '/api/users', 'POST', {'email': 'new@example.test', 'password': 'MemberPass123'})
        self.req(self.member, '/api/login', 'POST', {'email': 'new@example.test', 'password': 'MemberPass123'})
        self.assertEqual(self.req(self.member, '/api/role-options')[0], 403)
        self.assertEqual(self.req(self.member, '/api/projects'), (200, []))

    def test_json_content_type_size_and_host_boundaries(self):
        token = self.csrf_token(self.member)
        for headers, body, expected in [
            ({'Content-Type': 'text/plain'}, b'{}', 415),
            ({'Content-Type': 'application/json'}, b' ' * 16_385, 413),
            ({'Content-Type': 'application/json', 'Host': 'untrusted.example'}, b'{}', 403),
        ]:
            request = Request(self.base + '/api/setup', data=body, headers=headers)
            request.add_header('X-CSRF-Token', token)
            with self.assertRaises(HTTPError) as failure:
                self.member.open(request, timeout=3)
            with failure.exception as response:
                self.assertEqual(response.status, expected)
                self.assertIn('error', json.load(response))
        self.assertTrue(self.server.store.needs_setup())

    def test_overload_returns_json_503_and_retry_after(self):
        for _ in range(16):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPError) as failure:
                self.member.open(self.base + '/api/status', timeout=2)
            with failure.exception as response:
                self.assertEqual(response.status, 503)
                self.assertEqual(response.headers['Retry-After'], '1')
                self.assertIn('error', json.load(response))
        finally:
            for _ in range(16):
                self.server.slots.release()
        self.assertEqual(self.req(self.member, '/api/status')[0], 200)

    def test_slow_response_write_does_not_hold_database_lock(self):
        entered = threading.Event()
        release = threading.Event()
        results = []
        write_reply = Handler.write_reply

        def slow_write(handler, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return write_reply(handler, *args, **kwargs)

        with patch.object(Handler, 'write_reply', slow_write):
            worker = threading.Thread(target=lambda: results.append(self.req(self.admin, '/api/status')[0]))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.member.open(self.base + '/api/status', timeout=2) as response:
                    self.assertEqual(response.status, 200)
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(results, [200])

    def test_seventeen_connections_receive_overload_response(self):
        token = self.csrf_token(self.member)
        ready = threading.Condition()
        release = threading.Event()
        count = [0]
        connections = []
        payload = Handler.payload

        def slow_payload(handler):
            with ready:
                count[0] += 1
                ready.notify_all()
            release.wait(5)
            return payload(handler)

        with patch.object(Handler, 'payload', slow_payload):
            try:
                for _ in range(16):
                    connection = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2)
                    connections.append(connection)
                    connection.sendall(f'POST /api/login HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\nCookie: csrf={token}\r\nX-CSRF-Token: {token}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n'.encode())
                with ready:
                    self.assertTrue(ready.wait_for(lambda: count[0] == 16, timeout=3))
                with self.assertRaises(HTTPError) as failure:
                    self.member.open(self.base + '/api/status', timeout=2)
                with failure.exception as response:
                    self.assertEqual(response.status, 503)
                    self.assertEqual(response.headers['Retry-After'], '1')
                    self.assertIn('error', json.load(response))
            finally:
                release.set()
                for connection in connections:
                    connection.close()

    def test_loopback_client_ignores_system_proxy_configuration(self):
        with patch('urllib.request.getproxies', return_value={'http': 'http://127.0.0.1:1'}):
            client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            self.assertEqual(self.req(client, '/api/status'), (200, {'needs_setup': True}))

    def test_static_cache_and_unsupported_methods(self):
        with self.member.open(self.base + '/app.js') as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with self.member.open(self.base + '/favicon.ico') as response:
            self.assertEqual(response.status, 204)
        for method in ['PATCH', 'OPTIONS']:
            self.assertEqual(self.req(self.member, '/api/status', method)[0], 405)

    def test_corrupt_password_hash_is_not_a_client_validation_error(self):
        _, user = self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        with self.server.store_lock, self.server.store.db:
            self.server.store.db.execute('UPDATE users SET password_hash=? WHERE id=?', ('broken secret', user['id']))
        status, body = self.req(self.member, '/api/login', 'POST', {'email': user['email'], 'password': 'AdminPass123'})
        self.assertEqual(status, 500)
        self.assertNotIn('secret', body['error'])
        self.assertFalse(any(self.server.login_failures.values()))

    def test_cross_origin_write_is_rejected_before_setup(self):
        request = Request(self.base + '/api/setup', data=b'{}', headers={'Origin': 'https://untrusted.example'})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=2)
        with failure.exception as response:
            self.assertEqual(response.status, 403)
        self.assertTrue(self.server.store.needs_setup())

    def test_early_rejection_responses_remain_readable_with_pending_body(self):
        for body_size in (16_385, 256 * 1024):
            for _ in range(10):
                request = Request(self.base + '/api/setup', data=b'x' * body_size, headers={
                    'Content-Type': 'application/json',
                    'Origin': 'https://untrusted.example',
                })
                with self.assertRaises(HTTPError) as failure:
                    self.member.open(request, timeout=3)
                with failure.exception as response:
                    self.assertEqual(response.status, 403)
                    self.assertIn('error', json.load(response))
        self.assertTrue(self.server.store.needs_setup())

    def test_rejection_cleanup_obeys_total_deadline_with_continuous_data(self):
        for overload in (False, True):
            with self.subTest(overload=overload):
                now = [0.0]

                def drip(size):
                    now[0] += REJECT_DRAIN_TIMEOUT * 0.4
                    return b'x' * size

                connection = Mock()
                connection.recv.side_effect = drip
                handler = object.__new__(Handler)
                handler.connection = connection
                handler.wfile = Mock()
                handler.rfile = Mock()
                handler.rfile.read.side_effect = drip
                handler.rfile.read1.side_effect = drip
                handler.headers = Message()
                handler.headers['Content-Length'] = str(REJECT_DRAIN_LIMIT)
                with patch('server.time.monotonic', side_effect=lambda: now[0]):
                    if overload:
                        self.server.finish_rejected_socket(connection)
                        reader = connection.recv
                        connection.close.assert_called_once()
                    else:
                        handler.finish_rejected_body()
                        reader = handler.rfile.read1
                        handler.rfile.read.assert_not_called()
                self.assertEqual(reader.call_count, 3)
                timeouts = [call.args[0] for call in connection.settimeout.call_args_list]
                self.assertEqual(len(timeouts), 3)
                self.assertTrue(all(0 < value <= REJECT_DRAIN_TIMEOUT for value in timeouts))
                self.assertLess(timeouts[-1], timeouts[0])

    def test_csrf_missing_token_blocks_initialization_before_database_write(self):
        request = Request(self.base + '/api/setup', data=json.dumps({
            'email': 'admin@example.test', 'password': 'AdminPass123',
        }).encode(), headers={'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=3)
        with failure.exception as response:
            self.assertEqual(response.status, 403)
            self.assertIn('error', json.load(response))
        self.assertTrue(self.server.store.needs_setup())

    def test_csrf_rejects_forged_tokens_and_missing_cookie_even_without_origin(self):
        token = self.csrf_token(self.member)
        headers = {'Content-Type': 'application/json'}
        forged = '00' * 32 + '.' + '11' * 32
        for extra, opener in [
            ({'X-CSRF-Token': token}, build_opener(ProxyHandler({}))),
            ({'X-CSRF-Token': forged, 'Cookie': f'csrf={forged}'}, self.member),
            ({'X-CSRF-Token': token, 'Cookie': f'csrf={forged}'}, self.member),
            ({'X-CSRF-Token': token, 'Cookie': 'csrf="é"'}, self.member),
            ({'X-CSRF-Token': 'é'}, self.member),
            ({'X-CSRF-Token': 'invalid'}, self.member),
        ]:
            request = Request(self.base + '/api/setup', data=b'{}', headers={**headers, **extra})
            with self.assertRaises(HTTPError) as failure:
                opener.open(request, timeout=3)
            with failure.exception as response:
                self.assertEqual(response.status, 403)
        self.assertTrue(self.server.store.needs_setup())

    def test_csrf_covers_all_mutations_without_origin_and_preserves_database(self):
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        _, project = self.req(self.admin, '/api/projects', 'POST', {'name': 'Protected'})
        prefix = f"/api/projects/{project['id']}"
        with self.server.store_lock:
            before = list(self.server.store.db.iterdump())
            sessions = dict(self.server.sessions)
        for method, path in [
            ('POST', '/api/setup'), ('POST', '/api/login'), ('POST', '/api/logout'),
            ('POST', '/api/users'), ('POST', '/api/roles'), ('POST', '/api/projects'),
            ('PUT', '/api/roles/1/permissions'), ('POST', prefix + '/members'),
            ('DELETE', prefix + '/members/1'), ('POST', prefix + '/requirements'),
            ('PUT', prefix + '/requirements/1'), ('POST', prefix + '/requirements/1/archive'),
            ('POST', prefix + '/requirements/1/restore'), ('POST', prefix + '/tasks'),
            ('PUT', prefix + '/tasks/1'),
        ]:
            with self.subTest(method=method, path=path):
                request = Request(self.base + path, method=method, data=b'{}',
                                  headers={'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as failure:
                    self.admin.open(request, timeout=3)
                with failure.exception as response:
                    self.assertEqual(response.status, 403)
        with self.server.store_lock:
            self.assertEqual(list(self.server.store.db.iterdump()), before)
            self.assertEqual(self.server.sessions, sessions)
        self.assertEqual(self.req(self.admin, '/api/me')[0], 200)

    def test_csrf_tokens_are_session_bound_and_rotate_after_login_and_logout(self):
        before_login = self.csrf_token(self.admin)
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        stale = Request(self.base + '/api/logout', data=b'{}', headers={
            'Content-Type': 'application/json', 'X-CSRF-Token': before_login,
        })
        with self.assertRaises(HTTPError) as failure:
            self.admin.open(stale, timeout=3)
        failure.exception.close()
        self.assertEqual(failure.exception.code, 403)
        active = self.csrf_token(self.admin)
        self.assertNotEqual(active, before_login)
        transplanted = Request(self.base + '/api/logout', data=b'{}', headers={
            'Content-Type': 'application/json', 'X-CSRF-Token': active, 'Cookie': f'csrf={active}',
        })
        with self.assertRaises(HTTPError) as failure:
            build_opener(ProxyHandler({})).open(transplanted, timeout=3)
        failure.exception.close()
        self.assertEqual(failure.exception.code, 403)
        self.assertEqual(self.req(self.admin, '/api/logout', 'POST')[0], 200)
        self.assertNotEqual(self.csrf_token(self.admin), active)
        self.assertEqual(self.req(self.admin, '/api/login', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 200)

    def test_csrf_bootstrap_is_stable_private_and_rejects_cross_origin(self):
        with self.member.open(self.base + '/api/csrf', timeout=3) as response:
            token = json.load(response)['csrf_token']
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('HttpOnly', response.headers['Set-Cookie'])
            self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])
        self.assertEqual(self.csrf_token(self.member), token)
        request = Request(self.base + '/api/csrf', headers={'Origin': 'https://untrusted.example'})
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=3)
        failure.exception.close()
        self.assertEqual(failure.exception.code, 403)

    def test_csrf_old_server_signature_is_rejected_after_secret_rotation(self):
        token = self.csrf_token(self.member)
        self.server.csrf_secret = b'changed signing key for testing'
        request = Request(self.base + '/api/setup', data=b'{}', headers={
            'Content-Type': 'application/json', 'X-CSRF-Token': token,
        })
        with self.assertRaises(HTTPError) as failure:
            self.member.open(request, timeout=3)
        failure.exception.close()
        self.assertEqual(failure.exception.code, 403)
        self.assertNotEqual(self.csrf_token(self.member), token)
        self.assertEqual(self.req(self.member, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 201)


    def test_login_spraying_across_accounts_is_ip_throttled(self):
        """单账号限速键含账号名；同一 IP 跨账号喷洒由 IP 全局上限阻断。"""
        self.server.clock = lambda: 1000.0
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        with patch('server.LOGIN_IP_LIMIT', 3):
            for index in range(3):
                self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                          {'email': f'spray{index}@example.test', 'password': 'wrong'})[0], 401)
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 429)
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'spray3@example.test', 'password': 'wrong'})[0], 429)
            now = 1000.0 + LOGIN_WINDOW + 1
            self.server.clock = lambda: now
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 200)
        self.assertEqual(self.server.login_ip_failures, {})

    def test_successful_login_does_not_lift_ip_throttle_set_by_other_accounts(self):
        """被账号限速挡下的尝试不计数；跨账号失败在 IP 层持续累计并阻断正确密码。"""
        self.server.clock = lambda: 1000.0
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        with patch('server.LOGIN_IP_LIMIT', 6):
            for _ in range(5):
                self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                          {'email': 'spray@example.test', 'password': 'wrong'})[0], 401)
            # 单账号达到上限后进入指数退避，且不再累计失败
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'spray@example.test', 'password': 'wrong'})[0], 429)
            # 换一个全新账号仍可尝试并计入 IP 失败
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'other@example.test', 'password': 'wrong'})[0], 401)
            self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                      {'email': 'admin@example.test', 'password': 'AdminPass123'})[0], 429)

    def test_rate_limit_tables_stay_bounded_under_account_flooding(self):
        """伪造大量不存在的账号不会让限速表无限增长。"""
        self.req(self.admin, '/api/setup', 'POST', {'email': 'admin@example.test', 'password': 'AdminPass123'})
        with patch('server.LOGIN_TRACK_LIMIT', 2):
            for index in range(5):
                self.assertEqual(self.req(self.member, '/api/login', 'POST',
                                          {'email': f'burst{index}@example.test', 'password': 'wrong'})[0], 401)
        self.assertLessEqual(len(self.server.login_failures), 2)
        self.assertLessEqual(len(self.server.login_ip_failures), 2)

    def test_log_sanitizer_escapes_control_characters(self):
        self.assertEqual(sanitize_log('GET /x\r\nInjected: HTTP/1.1'),
                         'GET /x\\x0d\\x0aInjected: HTTP/1.1')
        self.assertEqual(sanitize_log('\x1b[31mred\x1b[0m\x7f'), '\\x1b[31mred\\x1b[0m\\x7f')
        self.assertEqual(sanitize_log('正常文本保持不变-蜂鸟'), '正常文本保持不变-蜂鸟')

    def test_responses_carry_cross_origin_isolation_headers(self):
        with self.member.open(self.base + '/api/status', timeout=2) as response:
            self.assertEqual(response.headers['Cross-Origin-Opener-Policy'], 'same-origin')
            self.assertEqual(response.headers['Cross-Origin-Resource-Policy'], 'same-origin')
        with self.member.open(self.base + '/', timeout=2) as response:
            self.assertEqual(response.headers['Cross-Origin-Opener-Policy'], 'same-origin')


if __name__ == '__main__': unittest.main()
