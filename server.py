"""本机教学 HTTP 服务。服务端负责认证与权限判定。"""
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlparse

from service import AppError, Store

ROOT = Path(__file__).resolve().parent
SESSIONS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        print('%s - %s' % (self.address_string(), format_string % args))

    @property
    def store(self):
        return self.server.store

    def reply(self, data, status=200, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie is not None:
            self.send_header('Set-Cookie', f'sid={cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise AppError('请求长度不合法。') from exc
        if length < 0 or length > 16_384:
            raise AppError('请求内容过大。', 413)
        try:
            value = json.loads(self.rfile.read(length) or b'{}')
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppError('JSON 格式不合法。') from exc
        if not isinstance(value, dict):
            raise AppError('请求必须是 JSON 对象。')
        return value

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        return cookie['sid'].value if 'sid' in cookie else None

    def user(self):
        user_id = SESSIONS.get(self.token())
        if not user_id:
            raise AppError('请先登录。', 401)
        return self.store.get_user(user_id)

    def user_with_capabilities(self, user):
        return {**user, **self.store.capabilities(user)}

    def serve_file(self, path):
        allowed = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                   '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
        if path not in allowed:
            raise AppError('页面不存在。', 404)
        name, mime = allowed[path]
        body = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', f'{mime}; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self, method):
        path = urlparse(self.path).path
        try:
            if method == 'GET' and not path.startswith('/api/'):
                return self.serve_file(path)
            if method == 'GET' and path == '/api/status':
                return self.reply({'needs_setup': self.store.needs_setup()})
            if method == 'POST' and path == '/api/setup':
                data = self.payload(); user = self.store.setup(data.get('email', ''), data.get('password', ''))
                token = secrets.token_urlsafe(32); SESSIONS[token] = user['id']
                return self.reply(user, 201, token)
            if method == 'POST' and path == '/api/login':
                data = self.payload(); user = self.store.authenticate(data.get('email', ''), data.get('password', ''))
                token = secrets.token_urlsafe(32); SESSIONS[token] = user['id']
                return self.reply(user, cookie=token)
            if method == 'POST' and path == '/api/logout':
                SESSIONS.pop(self.token(), None)
                return self.reply({'ok': True}, cookie='; Max-Age=0')

            user = self.user()
            if method == 'GET' and path == '/api/me':
                return self.reply(self.user_with_capabilities(user))
            if path == '/api/users':
                self.store.require_admin(user)
                if method == 'GET': return self.reply(self.store.list_users())
                if method == 'POST':
                    data = self.payload(); return self.reply(self.store.create_user(data.get('email', ''), data.get('password', '')), 201)
            if method == 'GET' and path == '/api/user-options':
                if not self.store.has_global_permission(user, 'members.manage'):
                    raise AppError('需要成员管理权限。', 403)
                return self.reply(self.store.list_users())
            if path == '/api/roles':
                if method == 'GET':
                    if not self.store.has_global_permission(user, 'roles.manage'):
                        raise AppError('需要角色管理权限。', 403)
                    return self.reply(self.store.list_roles())
                if method == 'POST':
                    if not self.store.has_global_permission(user, 'roles.manage'):
                        raise AppError('需要角色管理权限。', 403)
                    return self.reply(self.store.create_role(self.payload().get('name', '')), 201)
            if method == 'GET' and path == '/api/role-options':
                return self.reply([{'id': role['id'], 'name': role['name']} for role in self.store.list_roles()])
            match = re.fullmatch(r'/api/roles/(\d+)/permissions', path)
            if match and method == 'PUT':
                if not self.store.has_global_permission(user, 'roles.manage'):
                    raise AppError('需要角色管理权限。', 403)
                self.store.set_permissions(int(match.group(1)), self.payload().get('permissions'))
                return self.reply({'ok': True})
            if path == '/api/projects':
                if method == 'GET': return self.reply(self.store.list_projects(user))
                if method == 'POST':
                    if not self.store.has_global_permission(user, 'projects.manage'):
                        raise AppError('需要项目管理权限。', 403)
                    return self.reply(self.store.create_project(self.payload().get('name', '')), 201)
            match = re.fullmatch(r'/api/projects/(\d+)/manage-check', path)
            if match and method == 'GET':
                self.store.require_permission(user, int(match.group(1)), 'members.manage')
                return self.reply({'allowed': True, 'permission': 'members.manage'})
            match = re.fullmatch(r'/api/projects/(\d+)/members', path)
            if match:
                project_id = int(match.group(1))
                if method == 'GET':
                    self.store.require_permission(user, project_id, 'project.view')
                    return self.reply(self.store.list_members(project_id))
                if method == 'POST':
                    self.store.require_permission(user, project_id, 'members.manage')
                    data = self.payload()
                    self.store.add_member(project_id, int(data.get('user_id', 0)), int(data.get('role_id', 0)))
                    return self.reply({'ok': True}, 201)
            match = re.fullmatch(r'/api/projects/(\d+)/members/(\d+)', path)
            if match and method == 'DELETE':
                project_id, user_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'members.manage')
                self.store.remove_member(project_id, user_id)
                return self.reply({'ok': True})
            raise AppError('接口不存在。', 404)
        except AppError as exc:
            self.reply({'error': str(exc)}, exc.status)
        except (ValueError, TypeError):
            self.reply({'error': '参数不合法。'}, 400)

    def do_GET(self): self.handle_request('GET')
    def do_POST(self): self.handle_request('POST')
    def do_PUT(self): self.handle_request('PUT')
    def do_DELETE(self): self.handle_request('DELETE')


def create_server(db_path, port=8765):
    server = HTTPServer(('127.0.0.1', port), Handler)
    server.store = Store(db_path)
    return server


if __name__ == '__main__':
    data = ROOT / 'data'; data.mkdir(exist_ok=True)
    server = create_server(str(data / 'aimanager.db'))
    print('打开 http://127.0.0.1:8765')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.store.close(); server.server_close()
