"""本机教学 HTTP 服务。服务端负责认证与权限判定。"""
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

from service import AppError, Store

ROOT = Path(__file__).resolve().parent
SESSION_TTL = 2 * 60 * 60
LOGIN_WINDOW = 60
LOGIN_LIMIT = 5
SESSION_LIMIT = 5


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                body = b'{"error":"Server busy; retry later."}'
                request.settimeout(0.2)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\n'
                                b'Content-Type: application/json\r\n'
                                b'Retry-After: 1\r\nConnection: close\r\n'
                                + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
            except OSError:
                logging.warning('Overload response could not be delivered')
            finally:
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
    timeout = 2

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
        if getattr(self, '_defer_reply', False):
            self._response = (data, status, cookie)
            return
        self.write_reply(data, status, cookie)

    def write_reply(self, data, status=200, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie is not None:
            self.send_header('Set-Cookie', f'sid={cookie}; HttpOnly; SameSite=Strict; Path=/')
        if status == 429:
            self.send_header('Retry-After', str(self._retry_after))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        if hasattr(self, '_payload'):
            return self._payload
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get_all('Transfer-Encoding'):
            raise AppError('不支持传输编码，请使用 Content-Length。')
        if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]{1,10}', lengths[0])):
            raise AppError('请求长度不合法。')
        length = int(lengths[0]) if lengths else 0
        if length > 16_384:
            raise AppError('请求内容过大。', 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ConnectionAbortedError('Incomplete request body')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON key')
                result[key] = value
            return result

        def reject_constant(value):
            raise ValueError('Nonstandard JSON constant')

        try:
            value = json.loads(body or b'{}', object_pairs_hook=unique_object,
                               parse_constant=reject_constant)
        except (ValueError, RecursionError) as exc:
            raise AppError('JSON 格式不合法。') from exc
        if not isinstance(value, dict):
            raise AppError('请求必须是 JSON 对象。')
        return value

    def cookie_value(self, name):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except CookieError:
            return None
        return cookie[name].value if name in cookie else None

    def token(self):
        value = self.cookie_value('sid')
        return hashlib.sha256(value.encode()).hexdigest() if value else None

    def csrf_signature(self, nonce):
        binding = f'{self.token() or ""}:{nonce}'.encode()
        return hmac.new(self.server.csrf_secret, binding, hashlib.sha256).hexdigest()

    def valid_csrf(self, token):
        if not token or not re.fullmatch(r'[0-9a-f]{64}\.[0-9a-f]{64}', token):
            return False
        nonce, signature = token.split('.')
        return hmac.compare_digest(signature, self.csrf_signature(nonce))

    def serve_csrf(self):
        token = self.cookie_value('csrf')
        if not self.valid_csrf(token):
            nonce = secrets.token_hex(32)
            token = f'{nonce}.{self.csrf_signature(nonce)}'
        body = json.dumps({'csrf_token': token}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Set-Cookie', f'csrf={token}; HttpOnly; SameSite=Strict; Path=/')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def user(self):
        session = self.server.sessions.get(self.token())
        if session is None or session[1] <= self.server.clock():
            self.server.sessions.pop(self.token(), None)
            raise AppError('登录已失效，请重新登录。', 401)
        return self.store.get_user(session[0])

    def new_session(self, user):
        now = self.server.clock()
        self.server.sessions = {token: entry for token, entry in self.server.sessions.items() if entry[1] > now}
        self.server.sessions.pop(self.token(), None)
        existing = [key for key, entry in self.server.sessions.items() if entry[0] == user['id']]
        for key in existing[:max(0, len(existing) - SESSION_LIMIT + 1)]:
            del self.server.sessions[key]
        token = secrets.token_urlsafe(32)
        self.server.sessions[hashlib.sha256(token.encode()).hexdigest()] = (user['id'], now + SESSION_TTL)
        return token

    def user_with_capabilities(self, user):
        return {**user, **self.store.capabilities(user)}

    def serve_file(self, path):
        if path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        allowed = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                   '/app.js': ('app.js', 'text/javascript'), '/styles.css': ('styles.css', 'text/css')}
        if path not in allowed:
            raise AppError('页面不存在。', 404)
        name, mime = allowed[path]
        body = (ROOT / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', f'{mime}; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
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
            path = urlparse(self.path).path
            if (method != 'GET' or path == '/api/csrf') and origin is not None and origin != f'http://{host}':
                raise AppError('拒绝跨站写入请求。', 403)
            if method == 'GET' and path == '/api/csrf':
                return self.serve_csrf()
            if method in ('POST', 'PUT', 'DELETE'):
                token = self.headers.get('X-CSRF-Token', '')
                cookie = self.cookie_value('csrf') or ''
                if not self.valid_csrf(token) or not hmac.compare_digest(token.encode(), cookie.encode()):
                    raise AppError('安全令牌无效，请刷新页面后重试。', 403)
            # Do not hold the database lock while reading an untrusted slow body.
            if method in ('POST', 'PUT'):
                if self.headers.get_content_type() != 'application/json':
                    raise AppError('请求须使用 application/json。', 415)
                self._payload = self.payload()
            if method == 'GET' and not urlparse(self.path).path.startswith('/api/'):
                return self.serve_file(urlparse(self.path).path)
            with self.server.store_lock:
                self._defer_reply = True
                try:
                    self.dispatch_request(method)
                finally:
                    self._defer_reply = False
            self.write_reply(*self._response)
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
            if method == 'GET' and path == '/api/status':
                return self.reply({'needs_setup': self.store.needs_setup()})
            if method == 'POST' and path == '/api/setup':
                data = self.payload(); user = self.store.setup(data.get('email', ''), data.get('password', ''))
                token = self.new_session(user)
                return self.reply(user, 201, token)
            if method == 'POST' and path == '/api/login':
                now = self.server.clock()
                data = self.payload()
                for key, history in list(self.server.login_failures.items()):
                    while history and history[0] <= now - LOGIN_WINDOW:
                        history.popleft()
                    if not history:
                        del self.server.login_failures[key]
                email = data.get('email', '')
                account = email.strip().lower()[:120] if isinstance(email, str) else ''
                key = (account, self.client_address[0])
                failures = self.server.login_failures.setdefault(key, deque())
                if len(failures) >= LOGIN_LIMIT:
                    delay = min(LOGIN_WINDOW, 2 ** min(len(failures) - LOGIN_LIMIT, 6))
                    remaining = failures[-1] + delay - now
                    if remaining > 0:
                        self._retry_after = max(1, int(remaining + 0.999))
                        raise AppError('登录失败次数过多，请稍后重试。', 429)
                try:
                    user = self.store.authenticate(data.get('email', ''), data.get('password', ''))
                except AppError as error:
                    if error.status == 401:
                        failures.append(self.server.clock())
                    raise
                failures.clear()
                self.server.login_failures.pop(key, None)
                token = self.new_session(user)
                return self.reply(user, cookie=token)
            if method == 'POST' and path == '/api/logout':
                self.server.sessions.pop(self.token(), None)
                return self.reply({'ok': True}, cookie='; Max-Age=0')

            user = self.user()
            # Validate numeric route segments before int conversion and SQL binding.
            for identifier in re.findall(r'/(\d+)(?=/|$)', path):
                self.store._integer(identifier, '路径编号')
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
                return self.reply(self.store.user_options(user))
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
                return self.reply(self.store.role_options(user))
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
                    if set(data) - {'title', 'description', 'created_by'}:
                        raise AppError('需求创建字段不合法。')
                    return self.reply(self.store.create_requirement(
                        project_id, data.get('title', ''), data.get('description', ''), user['id']), 201)
                raise AppError('不支持的请求方法。', 405)
            match = re.fullmatch(r'/api/projects/(\d+)/requirements/(\d+)', path)
            if match:
                project_id, requirement_id = map(int, match.groups())
                self.store.require_permission(user, project_id,
                                              'project.view' if method == 'GET' else 'workitems.manage')
                if method == 'GET':
                    return self.reply(self.store.get_requirement(project_id, requirement_id))
                if method == 'PUT':
                    return self.reply(self.store.update_requirement(project_id, requirement_id, self.payload()))
                if method == 'DELETE':
                    raise AppError('需求不支持永久删除，请使用归档操作。', 405)
                raise AppError('不支持的请求方法。', 405)
            match = re.fullmatch(r'/api/projects/(\d+)/requirements/(\d+)/archive', path)
            if match:
                if method != 'POST':
                    raise AppError('不支持的请求方法。', 405)
                project_id, requirement_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'workitems.manage')
                data = self.payload()
                version = data.get('version')
                if version is None:
                    raise AppError('归档需求须携带版本号，请刷新后重试。', 428)
                return self.reply(self.store.archive_requirement(project_id, requirement_id, version))
            match = re.fullmatch(r'/api/projects/(\d+)/requirements/(\d+)/restore', path)
            if match:
                if method != 'POST':
                    raise AppError('不支持的请求方法。', 405)
                project_id, requirement_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'workitems.manage')
                data = self.payload()
                version = data.get('version')
                if version is None:
                    raise AppError('恢复需求须携带版本号，请刷新后重试。', 428)
                return self.reply(self.store.restore_requirement(project_id, requirement_id, version))
            match = re.fullmatch(r'/api/projects/(\d+)/tasks', path)
            if match:
                project_id = int(match.group(1))
                if method == 'GET':
                    self.store.require_permission(user, project_id, 'project.view')
                    return self.reply(self.store.list_tasks(project_id))
                if method == 'POST':
                    self.store.require_permission(user, project_id, 'workitems.manage')
                    data = self.payload()
                    if set(data) - {'title', 'requirement_id', 'assignee_user_id',
                                    'due_date', 'status', 'created_by'}:
                        raise AppError('任务创建字段不合法。')
                    return self.reply(self.store.create_task(
                        project_id, data.get('title', ''), data.get('requirement_id'),
                        data.get('assignee_user_id'), data.get('due_date'), user['id'],
                        data.get('status', 'backlog')), 201)
                raise AppError('不支持的请求方法。', 405)
            match = re.fullmatch(r'/api/projects/(\d+)/tasks/query', path)
            if match:
                if method != 'GET':
                    raise AppError('不支持的请求方法。', 405)
                project_id = int(match.group(1))
                self.store.require_permission(user, project_id, 'project.view')
                params = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                allowed = {'page', 'page_size', 'status', 'assignee_user_id',
                           'unassigned', 'requirement_id', 'keyword'}
                if any(key not in allowed or len(values) != 1 for key, values in params.items()):
                    raise AppError('查询参数未知或重复。')
                filters = {key: values[0] for key, values in params.items()}
                if 'unassigned' in filters:
                    if filters['unassigned'] not in ('true', 'false'):
                        raise AppError('unassigned 必须为 true 或 false。')
                    filters['unassigned'] = filters['unassigned'] == 'true'
                return self.reply(self.store.query_tasks(project_id, **filters))
            match = re.fullmatch(r'/api/projects/(\d+)/tasks/(\d+)', path)
            if match:
                project_id, task_id = map(int, match.groups())
                self.store.require_permission(user, project_id, 'workitems.manage')
                if method == 'PUT':
                    return self.reply(self.store.update_task(project_id, task_id, self.payload()))
                if method == 'DELETE':
                    raise AppError('当前版本不支持永久删除任务。', 405)
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
    def do_PATCH(self): self.reply({'error': '不支持的请求方法。'}, 405)
    def do_OPTIONS(self): self.reply({'error': '不支持的请求方法。'}, 405)


def create_server(db_path, port=8765):
    from collections import defaultdict

    server = LocalServer(('127.0.0.1', port), Handler)
    # ponytail: serialize this shared SQLite connection; use per-request connections if load grows.
    server.store_lock = threading.RLock()
    server.slots = threading.BoundedSemaphore(16)
    # ponytail: bounded process-local sessions expire on restart; persist only for restart continuity.
    server.sessions = {}
    server.csrf_secret = secrets.token_bytes(32)
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
