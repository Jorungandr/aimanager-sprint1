"""本机教学 HTTP 服务。服务端负责认证与权限判定。"""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlparse

from service import AppError, Store

ROOT = Path(__file__).resolve().parent
SESSION_TTL = 2 * 60 * 60
LOGIN_WINDOW = 60
LOGIN_LIMIT = 5


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    timeout = 5

    def end_headers(self):
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

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
        if hasattr(self, '_payload'):
            return self._payload
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise AppError('请求长度不合法。') from exc
        if length < 0 or length > 16_384:
            raise AppError('请求内容过大。', 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ConnectionAbortedError('Incomplete request body')
        try:
            value = json.loads(body or b'{}')
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
        session = self.server.sessions.get(self.token())
        if session is None or session[1] <= self.server.clock():
            self.server.sessions.pop(self.token(), None)
            raise AppError('登录已失效，请重新登录。', 401)
        return self.store.get_user(session[0])

    def new_session(self, user):
        now = self.server.clock()
        self.server.sessions = {token: entry for token, entry in self.server.sessions.items() if entry[1] > now}
        token = secrets.token_urlsafe(32)
        self.server.sessions[token] = (user['id'], now + SESSION_TTL)
        return token

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
        try:
            host = self.headers.get('Host', '')
            allowed_hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if host not in allowed_hosts:
                raise AppError('仅允许本机地址访问。', 403)
            origin = self.headers.get('Origin')
            if method != 'GET' and origin is not None and origin != f'http://{host}':
                raise AppError('拒绝跨站写入请求。', 403)
            # Do not hold the database lock while reading an untrusted slow body.
            if method in ('POST', 'PUT'):
                self._payload = self.payload()
            with self.server.store_lock:
                self.dispatch_request(method)
        except AppError as exc:
            self.reply({'error': str(exc)}, exc.status)
        except (ConnectionError, TimeoutError):
            logging.warning('Request interrupted or timed out: %s', self.path)
        except Exception:
            logging.exception('Unhandled request error: %s %s', method, self.path)
            self.reply({'error': '服务器内部错误，请稍后重试。'}, 500)

    def dispatch_request(self, method):
        path = urlparse(self.path).path
        try:
            if method == 'GET' and not path.startswith('/api/'):
                return self.serve_file(path)
            if method == 'GET' and path == '/api/status':
                return self.reply({'needs_setup': self.store.needs_setup()})
            if method == 'POST' and path == '/api/setup':
                data = self.payload(); user = self.store.setup(data.get('email', ''), data.get('password', ''))
                token = self.new_session(user)
                return self.reply(user, 201, token)
            if method == 'POST' and path == '/api/login':
                now = self.server.clock()
                failures = self.server.login_failures[self.client_address[0]]
                while failures and failures[0] <= now - LOGIN_WINDOW:
                    failures.popleft()
                if len(failures) >= LOGIN_LIMIT:
                    raise AppError('登录失败次数过多，请一分钟后重试。', 429)
                data = self.payload()
                try:
                    user = self.store.authenticate(data.get('email', ''), data.get('password', ''))
                except AppError:
                    failures.append(now)
                    raise
                token = self.new_session(user)
                return self.reply(user, cookie=token)
            if method == 'POST' and path == '/api/logout':
                self.server.sessions.pop(self.token(), None)
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
                self.store.set_permissions(int(match.group(1)), self.payload().get('permissions'), actor=user)
                return self.reply({'ok': True})
            if path == '/api/projects':
                if method == 'GET': return self.reply(self.store.list_projects(user))
                if method == 'POST':
                    if not self.store.has_global_permission(user, 'projects.manage'):
                        raise AppError('需要项目管理权限。', 403)
                    return self.reply(self.store.create_project(self.payload().get('name', ''), creator=user), 201)
            match = re.fullmatch(r'/api/projects/(\d+)/role-options', path)
            if match and method == 'GET':
                return self.reply([{'id': role['id'], 'name': role['name']}
                                   for role in self.store.assignable_roles(user, int(match.group(1)))])
            match = re.fullmatch(r'/api/projects/(\d+)/manage-check', path)
            if match and method == 'GET':
                self.store.require_permission(user, int(match.group(1)), 'members.manage')
                return self.reply({'allowed': True, 'permission': 'members.manage'})
            match = re.fullmatch(r'/api/projects/(\d+)/workitems/manage-check', path)
            if match and method == 'GET':
                self.store.require_permission(user, int(match.group(1)), 'workitems.manage')
                return self.reply({'allowed': True, 'permission': 'workitems.manage'})
            match = re.fullmatch(r'/api/projects/(\d+)/requirements', path)
            if match:
                project_id = int(match.group(1))
                if method == 'GET':
                    self.store.require_permission(user, project_id, 'project.view')
                    return self.reply(self.store.list_requirements(project_id))
                if method == 'POST':
                    self.store.require_permission(user, project_id, 'workitems.manage')
                    data = self.payload()
                    return self.reply(self.store.create_requirement(
                        project_id, data.get('title', ''), data.get('description', ''), user['id']), 201)
            match = re.fullmatch(r'/api/projects/(\d+)/tasks', path)
            if match:
                project_id = int(match.group(1))
                if method == 'GET':
                    self.store.require_permission(user, project_id, 'project.view')
                    return self.reply(self.store.list_tasks(project_id))
                if method == 'POST':
                    self.store.require_permission(user, project_id, 'workitems.manage')
                    data = self.payload()
                    return self.reply(self.store.create_task(
                        project_id, data.get('title', ''), data.get('requirement_id'),
                        data.get('assignee_user_id'), data.get('due_date'), user['id'],
                        data.get('status', 'backlog')), 201)
            match = re.fullmatch(r'/api/projects/(\d+)/tasks/(\d+)', path)
            if match and method == 'PUT':
                project_id, task_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'workitems.manage')
                return self.reply(self.store.update_task(project_id, task_id, self.payload()))
            match = re.fullmatch(r'/api/projects/(\d+)/members', path)
            if match:
                project_id = int(match.group(1))
                if method == 'GET':
                    self.store.require_permission(user, project_id, 'project.view')
                    return self.reply(self.store.list_members(project_id))
                if method == 'POST':
                    self.store.require_permission(user, project_id, 'members.manage')
                    data = self.payload()
                    self.store.add_member(project_id, data.get('user_id', 0), data.get('role_id', 0), actor=user)
                    return self.reply({'ok': True}, 201)
            match = re.fullmatch(r'/api/projects/(\d+)/members/(\d+)', path)
            if match and method == 'DELETE':
                project_id, user_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'members.manage')
                self.store.remove_member(project_id, user_id, actor=user)
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
    from collections import defaultdict

    server = LocalServer(('127.0.0.1', port), Handler)
    server.store_lock = threading.RLock()
    server.slots = threading.BoundedSemaphore(16)
    server.sessions = {}
    server.login_failures = defaultdict(deque)
    server.clock = time.monotonic
    server.store = Store(db_path)
    return server


if __name__ == '__main__':
    data = ROOT / 'data'; data.mkdir(exist_ok=True)
    server = create_server(str(data / 'aimanager.db'))
    print('打开 http://127.0.0.1:8765')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); server.store.close()
