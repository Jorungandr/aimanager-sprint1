"""直接请求 API，验证前端之外的越权阻断。"""
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import SESSIONS, create_server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.tmp.name) / 'api.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(HTTPCookieProcessor(CookieJar()))
        self.member = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.store.close(); self.server.server_close()
        SESSIONS.clear(); self.tmp.cleanup()

    def req(self, opener, path, method='GET', data=None):
        body = json.dumps(data or {}).encode() if method in ('POST', 'PUT') else None
        request = Request(self.base + path, data=body, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            with opener.open(request) as response:
                return response.status, json.load(response)
        except HTTPError as error:
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
        self.assertEqual(self.req(self.member, '/api/roles', 'POST', {'name': '伪管理员'})[0], 403)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/manage-check")[0], 403)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members", 'POST',
                                  {'user_id': user['id'], 'role_id': role['id']})[0], 403)
        self.assertEqual(self.req(self.admin, f"/api/projects/{project['id']}/members/{user['id']}", 'DELETE')[0], 200)
        self.assertEqual(self.req(self.member, f"/api/projects/{project['id']}/members")[0], 403)


if __name__ == '__main__': unittest.main()
