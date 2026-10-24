"""Sprint 1 权限基线、Sprint 2 协作增量与 Sprint 3 统计追溯。查询统一使用参数绑定。"""
from datetime import date, datetime, timedelta
import hashlib
import hmac
import os
import re
import sqlite3

from database import DatabaseVersionError, SCHEMA_VERSION, open_database

PERMISSIONS = ('project.view', 'members.manage', 'projects.manage', 'roles.manage',
               'workitems.manage')
TASK_STATUSES = ('backlog', 'todo', 'doing', 'done')
PROJECT_PERMISSIONS = {'project.view', 'members.manage', 'workitems.manage'}
PASSWORD_ITERATIONS = 600_000
MAX_PASSWORD_ITERATIONS = 2_000_000


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, path):
        try:
            self.db = open_database(path)
        except DatabaseVersionError as exc:
            raise AppError(str(exc), 409) from exc

    def close(self):
        self.db.close()

    def _one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    # ---------- 变更事件：任务、需求与成员角色的统一追溯链 ----------
    @staticmethod
    def _event_value(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return '1' if value else '0'
        return str(value)

    def _record_event(self, project_id, entity_type, entity_id, action,
                      field=None, before=None, after=None, actor=None):
        """在写事务内登记一条变更；调用方负责事务边界。"""
        self.db.execute('''INSERT INTO audit_events(
            project_id,entity_type,entity_id,action,field,from_value,to_value,actor_user_id)
            VALUES(?,?,?,?,?,?,?,?)''',
            (project_id, entity_type, entity_id, action, field,
             self._event_value(before), self._event_value(after), actor))

    def _record_task_changes(self, current, updated, actor):
        for field in ('title', 'requirement_id', 'assignee_user_id', 'due_date', 'status', 'position'):
            before, after = current.get(field), updated.get(field)
            if before != after:
                self._record_event(current['project_id'], 'task', current['id'], 'update',
                                   field, before, after, actor)

    def list_audit(self, project_id, entity_type=None, entity_id=None, limit=20):
        """按项目读取变更记录；只读接口，调用方须先校验 project.view。"""
        project_id = self._integer(project_id, '项目编号')
        self._project_exists(project_id)
        if entity_type is not None and entity_type not in ('task', 'requirement', 'membership'):
            raise AppError('变更对象类型不合法。')
        limit = self._integer(limit, '返回条数', minimum=1)
        if limit > 100:
            raise AppError('单次最多返回 100 条变更记录。')
        clauses, params = ['e.project_id=?'], [project_id]
        if entity_type is not None:
            clauses.append('e.entity_type=?')
            params.append(entity_type)
        if entity_id is not None:
            clauses.append('e.entity_id=?')
            params.append(self._integer(entity_id, '对象编号'))
        rows = self.db.execute('''SELECT e.id,e.entity_type,e.entity_id,e.action,e.field,
                e.from_value,e.to_value,e.created_at,u.email AS actor_email,
                COALESCE(t.title,r.title,mu.email) AS entity_label
            FROM audit_events e LEFT JOIN users u ON u.id=e.actor_user_id
            LEFT JOIN tasks t ON e.entity_type='task' AND t.id=e.entity_id
            LEFT JOIN requirements r ON e.entity_type='requirement' AND r.id=e.entity_id
            LEFT JOIN users mu ON e.entity_type='membership' AND mu.id=e.entity_id
            WHERE ''' + ' AND '.join(clauses) + ' ORDER BY e.id DESC LIMIT ?',
            [*params, limit]).fetchall()
        return [dict(row) for row in rows]

    def needs_setup(self):
        return self._one('SELECT COUNT(*) AS n FROM users')['n'] == 0

    @staticmethod
    def _hash(password, salt=None, iterations=PASSWORD_ITERATIONS):
        salt = salt or os.urandom(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
        return f'pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}'

    def create_user(self, email, password, admin=False):
        email = self._short_text(email, '邮箱', 120).lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 120:
            raise AppError('请输入有效邮箱。')
        if not isinstance(password, str) or len(password) < 8 or len(password) > 128:
            raise AppError('密码长度应为 8 至 128 个字符。')
        try:
            with self.db:
                cur = self.db.execute('INSERT INTO users(email,password_hash,is_admin) VALUES(?,?,?)',
                                      (email, self._hash(password), int(admin)))
            return self.get_user(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise AppError('邮箱已存在。') from exc

    def setup(self, email, password):
        if not self.needs_setup():
            raise AppError('管理员已初始化。', 409)
        return self.create_user(email, password, admin=True)

    def authenticate(self, email, password):
        if not isinstance(email, str) or not isinstance(password, str) or len(password) > 128:
            raise AppError('邮箱或密码错误。', 401)
        row = self._one('SELECT * FROM users WHERE email=?', (email.strip().lower(),))
        if not row:
            self._hash(password, b'\0' * 16)
            raise AppError('邮箱或密码错误。', 401)
        try:
            stored = row['password_hash']
            legacy = ':' in stored and '$' not in stored
            if legacy:
                salt, expected = stored.split(':')
                iterations = 150_000
            else:
                algorithm, count, salt, expected = stored.split('$')
                if algorithm != 'pbkdf2_sha256' or not re.fullmatch(r'[1-9][0-9]{0,6}', count):
                    raise ValueError('Unsupported credential parameters')
                iterations = int(count)
            # Bound stored work factors before hashing, including malformed database values.
            if not 100_000 <= iterations <= MAX_PASSWORD_ITERATIONS:
                raise ValueError('Unsupported credential work factor')
            if not re.fullmatch(r'[0-9a-f]{32}', salt) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('Invalid credential format')
            salt = bytes.fromhex(salt)
        except (ValueError, TypeError, AttributeError) as exc:
            raise AppError('账号凭证异常，请联系管理员。', 500) from exc
        actual = self._hash(password, salt, iterations).rsplit('$', 1)[1]
        if not hmac.compare_digest(actual, expected):
            raise AppError('邮箱或密码错误。', 401)
        if legacy or iterations < PASSWORD_ITERATIONS:
            upgraded = self._hash(password)
            with self.db:
                result = self.db.execute('UPDATE users SET password_hash=? WHERE id=? AND password_hash=?',
                                         (upgraded, row['id'], stored))
                if result.rowcount != 1:
                    raise AppError('账号凭证已变更，请重新登录。', 401)
        return self.get_user(row['id'])

    def get_user(self, user_id):
        row = self._one('SELECT id,email,is_admin FROM users WHERE id=?', (user_id,))
        if not row:
            raise AppError('用户不存在。', 404)
        return {'id': row['id'], 'email': row['email'], 'is_admin': bool(row['is_admin'])}

    def list_users(self):
        return [dict(row) for row in self.db.execute('SELECT id,email FROM users ORDER BY id')]

    def user_options(self, user):
        if user['is_admin']:
            return self.list_users()
        managed = [project['id'] for project in self.list_projects(user)
                   if self.has_permission(user, project['id'], 'members.manage')]
        visible = {user['id']}
        for project_id in managed:
            visible.update(member['user_id'] for member in self.list_members(project_id))
        return [dict(row) for row in self.db.execute('SELECT id,email FROM users ORDER BY id')
                if row['id'] in visible]

    def role_options(self, user):
        if self.has_global_permission(user, 'roles.manage'):
            return [{'id': role['id'], 'name': role['name']} for role in self.list_roles()]
        projects = self.list_projects(user)
        if not projects:
            raise AppError('需要可访问的项目。', 403)
        visible = set()
        for project in projects:
            visible.update(row['role_id'] for row in self.db.execute(
                'SELECT role_id FROM memberships WHERE project_id=?', (project['id'],)))
        return [{'id': role['id'], 'name': role['name']} for role in self.list_roles()
                if role['id'] in visible]

    def require_admin(self, user):
        if not user['is_admin']:
            raise AppError('需要管理员权限。', 403)

    def has_global_permission(self, user, permission):
        """检查不隶属于单个项目的管理权限。

        角色仍通过项目成员关系授予；拥有任一项目中该权限的成员可执行
        对应的平台级管理操作。管理员保留全部权限，避免初始化和救援流程
        被角色配置反向锁死。
        """
        if user['is_admin']:
            return True
        if permission == 'members.manage' and self._one('SELECT 1 FROM projects WHERE owner_user_id=?', (user['id'],)):
            return True
        return self._one('''SELECT 1 FROM memberships m JOIN role_permissions rp ON rp.role_id=m.role_id
            WHERE m.user_id=? AND rp.permission=?''', (user['id'], permission)) is not None

    def capabilities(self, user):
        return {
            'can_manage_roles': self.has_global_permission(user, 'roles.manage'),
            'can_manage_projects': self.has_global_permission(user, 'projects.manage'),
            'can_manage_members': self.has_global_permission(user, 'members.manage'),
        }

    def create_role(self, name):
        name = self._short_text(name, '角色名称', 30)
        try:
            with self.db:
                cur = self.db.execute('INSERT INTO roles(name) VALUES(?)', (name,))
            return {'id': cur.lastrowid, 'name': name, 'permissions': []}
        except sqlite3.IntegrityError as exc:
            raise AppError('角色名称已存在。') from exc

    def list_roles(self):
        roles = {}
        # One query regardless of role count; retain empty roles and stable ordering.
        for row in self.db.execute('''SELECT r.id,r.name,rp.permission FROM roles r
                LEFT JOIN role_permissions rp ON rp.role_id=r.id ORDER BY r.id,rp.permission'''):
            role = roles.setdefault(row['id'], {'id': row['id'], 'name': row['name'], 'permissions': []})
            if row['permission'] is not None:
                role['permissions'].append(row['permission'])
        return list(roles.values())

    def set_permissions(self, role_id, permissions, actor=None):
        if not self._one('SELECT id FROM roles WHERE id=?', (role_id,)):
            raise AppError('角色不存在。', 404)
        if not isinstance(permissions, list) or any(p not in PERMISSIONS for p in permissions):
            raise AppError('权限项不合法。')
        permissions = set(permissions)
        if permissions:
            permissions.add('project.view')
        if actor is not None:
            if not self.has_global_permission(actor, 'roles.manage'):
                raise AppError('需要角色管理权限。', 403)
            existing = {row['permission'] for row in self.db.execute(
                'SELECT permission FROM role_permissions WHERE role_id=?', (role_id,))}
            if not actor['is_admin']:
                ceiling = {p for p in PERMISSIONS if self.has_global_permission(actor, p)}
                if not (permissions | existing).issubset(ceiling):
                    raise AppError('不能修改或授予超出本人授权范围的角色权限。', 403)
        with self.db:
            self.db.execute('DELETE FROM role_permissions WHERE role_id=?', (role_id,))
            self.db.executemany('INSERT INTO role_permissions(role_id,permission) VALUES(?,?)',
                                [(role_id, p) for p in set(permissions)])

    def create_project(self, name, creator=None):
        name = self._short_text(name, '项目名称', 60)
        if creator is not None and not self.has_global_permission(creator, 'projects.manage'):
            raise AppError('需要项目管理权限。', 403)
        with self.db:
            cur = self.db.execute('INSERT INTO projects(name,owner_user_id) VALUES(?,?)',
                                  (name, creator['id'] if creator else None))
        return {'id': cur.lastrowid, 'name': name}

    def has_permission(self, user, project_id, permission):
        if user['is_admin']:
            return self._one('SELECT id FROM projects WHERE id=?', (project_id,)) is not None
        if permission in PROJECT_PERMISSIONS and self.is_owner(project_id, user['id']):
            return True
        return self._one('''SELECT 1 FROM memberships m JOIN role_permissions rp ON rp.role_id=m.role_id
            WHERE m.project_id=? AND m.user_id=? AND rp.permission=?''',
            (project_id, user['id'], permission)) is not None

    def require_permission(self, user, project_id, permission):
        if user['is_admin']:
            self._project_exists(project_id)
        if not self.has_permission(user, project_id, permission):
            raise AppError('无权访问该项目或执行此操作。', 403)

    def list_projects(self, user):
        if user['is_admin']:
            rows = self.db.execute('SELECT id,name FROM projects ORDER BY id')
        else:
            rows = self.db.execute('''SELECT p.id,p.name FROM projects p WHERE p.owner_user_id=? OR EXISTS (
                SELECT 1 FROM memberships m JOIN role_permissions rp ON rp.role_id=m.role_id
                WHERE m.project_id=p.id AND m.user_id=? AND rp.permission='project.view') ORDER BY p.id''',
                (user['id'], user['id']))
        return [dict(row) for row in rows]

    def is_owner(self, project_id, user_id):
        return self._one('SELECT 1 FROM projects WHERE id=? AND owner_user_id=?', (project_id, user_id)) is not None

    def assignable_roles(self, actor, project_id):
        self.require_permission(actor, project_id, 'members.manage')
        ceiling = {p for p in PERMISSIONS if self.has_permission(actor, project_id, p)}
        return [role for role in self.list_roles()
                if actor['is_admin'] or set(role['permissions']).issubset(ceiling)]

    def add_member(self, project_id, user_id, role_id, actor=None):
        user_id = self._integer(user_id, '用户编号')
        role_id = self._integer(role_id, '角色编号')
        if self.is_owner(project_id, user_id):
            raise AppError('项目创建者具有固定的项目管理权，不能通过成员角色覆盖。', 409)
        if actor is not None:
            allowed = {role['id'] for role in self.assignable_roles(actor, project_id)}
            if role_id not in allowed:
                raise AppError('不能指派超出本人项目授权范围的角色。', 403)
            current = self._one('SELECT role_id FROM memberships WHERE project_id=? AND user_id=?', (project_id, user_id))
            if current and current['role_id'] not in allowed:
                raise AppError('不能修改高于本人授权范围的成员角色。', 403)
        if not self._one('SELECT id FROM projects WHERE id=?', (project_id,)):
            raise AppError('项目不存在。', 404)
        if not self._one('SELECT id FROM users WHERE id=?', (user_id,)):
            raise AppError('用户不存在。', 404)
        if not self._one('SELECT id FROM roles WHERE id=?', (role_id,)):
            raise AppError('角色不存在。', 404)
        previous = self._one('SELECT role_id FROM memberships WHERE project_id=? AND user_id=?',
                             (project_id, user_id))
        actor_id = actor['id'] if actor else None
        with self.db:
            self.db.execute('''INSERT INTO memberships(project_id,user_id,role_id) VALUES(?,?,?)
                ON CONFLICT(project_id,user_id) DO UPDATE SET role_id=excluded.role_id''',
                (project_id, user_id, role_id))
            if previous is None:
                self._record_event(project_id, 'membership', user_id, 'create',
                                   'role_id', None, role_id, actor_id)
            elif previous['role_id'] != role_id:
                self._record_event(project_id, 'membership', user_id, 'update',
                                   'role_id', previous['role_id'], role_id, actor_id)

    def list_members(self, project_id):
        return [dict(row) for row in self.db.execute('''SELECT u.id AS user_id,u.email,r.name AS role_name,0 AS is_owner
            FROM memberships m JOIN users u ON u.id=m.user_id JOIN roles r ON r.id=m.role_id
            WHERE m.project_id=? AND NOT EXISTS(SELECT 1 FROM projects p WHERE p.id=m.project_id AND p.owner_user_id=m.user_id)
            UNION ALL SELECT u.id,u.email,'项目创建者',1 FROM projects p JOIN users u ON u.id=p.owner_user_id
            WHERE p.id=? ORDER BY email''', (project_id, project_id))]

    def remove_member(self, project_id, user_id, actor=None):
        if self.is_owner(project_id, user_id):
            raise AppError('不能移除项目创建者，请保留项目管理入口。', 409)
        current = self._one('SELECT role_id FROM memberships WHERE project_id=? AND user_id=?',
                            (project_id, user_id))
        if actor is not None:
            allowed = {role['id'] for role in self.assignable_roles(actor, project_id)}
            if current and current['role_id'] not in allowed:
                raise AppError('不能移除高于本人授权范围的成员。', 403)
        with self.db:
            cur = self.db.execute('DELETE FROM memberships WHERE project_id=? AND user_id=?',
                                  (project_id, user_id))
            # Preserve work items but release assignments when project access is revoked.
            if cur.rowcount:
                self.db.execute('UPDATE tasks SET assignee_user_id=NULL,version=version+1 WHERE project_id=? AND assignee_user_id=?',
                                (project_id, user_id))
                self._record_event(project_id, 'membership', user_id, 'remove', 'role_id',
                                   current['role_id'] if current else None, None,
                                   actor['id'] if actor else None)
        if not cur.rowcount:
            raise AppError('成员关系不存在。', 404)
    def _project_exists(self, project_id):
        if not self._one('SELECT id FROM projects WHERE id=?', (project_id,)):
            raise AppError('项目不存在。', 404)

    @staticmethod
    def _short_text(value, label, limit):
        if not isinstance(value, str):
            raise AppError(f'{label}必须是文本。')
        value = value.strip()
        if not value or len(value) > limit:
            raise AppError(f'{label}应为 1 至 {limit} 字。')
        return value

    @staticmethod
    def _due_date(value):
        if value in (None, ''):
            return None
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise AppError('截止日期必须为 YYYY-MM-DD。')
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise AppError('截止日期必须为 YYYY-MM-DD。') from exc

    @staticmethod
    def _integer(value, label, minimum=1):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise AppError(f'{label}必须为整数。')
        if isinstance(value, str) and not re.fullmatch(r'\d+', value):
            raise AppError(f'{label}必须为整数。')
        if isinstance(value, str) and len(value) > 19:
            raise AppError(f'{label}超出允许范围。')
        number = int(value)
        if not minimum <= number <= 2**63 - 1:
            raise AppError(f'{label}超出允许范围。')
        return number
    def list_requirements(self, project_id, include_archived=True):
        """Include history by default; task-creation selectors may request active items only."""
        self._project_exists(project_id)
        if not isinstance(include_archived, bool):
            raise AppError('是否包含归档需求必须为布尔值。')
        query = '''
            SELECT id,project_id,title,description,created_by,created_at,version,archived
            FROM requirements WHERE project_id=?'''
        if not include_archived:
            query += ' AND archived=0'
        return [{**dict(row), 'archived': bool(row['archived'])}
                for row in self.db.execute(query + ' ORDER BY id', (project_id,))]

    @staticmethod
    def _requirement_description(value):
        if not isinstance(value, str):
            raise AppError('需求说明必须是文本。')
        value = value.strip()
        if len(value) > 500:
            raise AppError('需求说明不得超过 500 字。')
        return value

    def create_requirement(self, project_id, title, description, created_by):
        self._project_exists(project_id)
        title = self._short_text(title, '需求标题', 80)
        description = self._requirement_description(description)
        self.get_user(created_by)
        with self.db:
            cur = self.db.execute('''INSERT INTO requirements(project_id,title,description,created_by)
                VALUES(?,?,?,?)''', (project_id, title, description, created_by))
            self._record_event(project_id, 'requirement', cur.lastrowid, 'create',
                               'title', None, title, created_by)
        return self.get_requirement(project_id, cur.lastrowid)

    def get_requirement(self, project_id, requirement_id):
        project_id = self._integer(project_id, '项目编号')
        requirement_id = self._integer(requirement_id, '需求编号')
        row = self._one('''SELECT id,project_id,title,description,created_by,created_at,version,archived
            FROM requirements WHERE project_id=? AND id=?''', (project_id, requirement_id))
        if not row:
            raise AppError('需求不存在或不属于当前项目。', 404)
        return {**dict(row), 'archived': bool(row['archived'])}

    @staticmethod
    def _requirement_version(current, version):
        if version is None:
            raise AppError('修改需求须携带读取时的版本号，请刷新后重试。', 428)
        version = Store._integer(version, '需求版本')
        if version != current['version']:
            raise AppError('需求已被其他操作修改，请刷新后重试。', 409)
        return version

    def _save_requirement(self, current, version, title, description, archived,
                          action='update', actor=None):
        # Compare-and-swap guards edits and archive operations across connections.
        with self.db:
            result = self.db.execute('''UPDATE requirements SET title=?,description=?,archived=?,version=version+1
                WHERE project_id=? AND id=? AND version=?''',
                (title, description, int(archived), current['project_id'], current['id'], version))
            if result.rowcount != 1:
                raise AppError('需求已被其他操作修改，请刷新后重试。', 409)
            if action == 'update':
                for field, before, after in (('title', current['title'], title),
                                             ('description', current['description'], description)):
                    if before != after:
                        self._record_event(current['project_id'], 'requirement', current['id'],
                                           'update', field, before, after, actor)
            else:
                self._record_event(current['project_id'], 'requirement', current['id'],
                                   action, 'archived', current['archived'], archived, actor)
        return self.get_requirement(current['project_id'], current['id'])

    def update_requirement(self, project_id, requirement_id, changes, actor=None):
        """Edit title/description with version. API callers must check workitems.manage."""
        current = self.get_requirement(project_id, requirement_id)
        if not isinstance(changes, dict) or any(key not in {'title', 'description', 'version'} for key in changes):
            raise AppError('需求更新字段不合法。')
        version = self._requirement_version(current, changes.get('version'))
        if not {'title', 'description'}.intersection(changes):
            raise AppError('至少提供需求标题或说明。')
        if current['archived']:
            raise AppError('归档需求不能编辑，请先恢复。', 409)
        title = self._short_text(changes.get('title', current['title']), '需求标题', 80)
        description = self._requirement_description(changes.get('description', current['description']))
        return self._save_requirement(current, version, title, description, False, actor=actor)

    def _set_requirement_archived(self, project_id, requirement_id, version, archived, actor=None):
        current = self.get_requirement(project_id, requirement_id)
        version = self._requirement_version(current, version)
        if current['archived'] == archived:
            return current
        return self._save_requirement(current, version, current['title'], current['description'], archived,
                                      action='archive' if archived else 'restore', actor=actor)

    def archive_requirement(self, project_id, requirement_id, version=None, actor=None):
        """Archive without deleting linked tasks; a matching repeated call is a no-op."""
        return self._set_requirement_archived(project_id, requirement_id, version, True, actor)

    def restore_requirement(self, project_id, requirement_id, version=None, actor=None):
        """Restore an archived requirement, checking the same optimistic version."""
        return self._set_requirement_archived(project_id, requirement_id, version, False, actor)

    def _requirement_link(self, project_id, requirement_id, previous_id=None):
        if isinstance(requirement_id, bool) or isinstance(requirement_id, float):
            raise AppError('需求编号必须为整数。')
        if requirement_id in (None, '', 0, '0'):
            return None
        requirement_id = self._integer(requirement_id, '需求编号')
        requirement = self.get_requirement(project_id, requirement_id)
        if requirement['archived'] and requirement_id != previous_id:
            raise AppError('不能新关联已归档需求，请先恢复需求。', 409)
        return requirement_id

    def list_tasks(self, project_id):
        self._project_exists(project_id)
        return [dict(row) for row in self.db.execute('''
            SELECT t.id,t.project_id,t.requirement_id,t.title,t.assignee_user_id,
                   u.email AS assignee_email,t.due_date,t.status,t.position,t.created_by,t.created_at,t.version
            FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE t.project_id=?
            ORDER BY CASE t.status WHEN 'backlog' THEN 0 WHEN 'todo' THEN 1
                      WHEN 'doing' THEN 2 ELSE 3 END, t.position, t.id''', (project_id,))]

    def query_tasks(self, project_id, *, assignee_user_id=None, unassigned=False,
                    status=None, requirement_id=None, keyword='', page=1, page_size=20):
        """Project-scoped filters and pagination; API callers must check project.view."""
        project_id = self._integer(project_id, '项目编号')
        self._project_exists(project_id)
        page = self._integer(page, '页码')
        page_size = self._integer(page_size, '每页数量')
        if page > 1_000_000 or page_size > 100:
            raise AppError('页码不得超过1000000，每页数量不得超过100。')
        if not isinstance(unassigned, bool) or (unassigned and assignee_user_id is not None):
            raise AppError('未指派筛选必须为布尔值，且不能同时指定负责人。')
        if not isinstance(keyword, str) or len(keyword) > 100:
            raise AppError('搜索关键词必须为不超过100字的文本。')
        clauses, params = ['t.project_id=?'], [project_id]
        if status is not None:
            if status not in TASK_STATUSES:
                raise AppError('任务状态不合法。')
            clauses.append('t.status=?')
            params.append(status)
        if unassigned:
            clauses.append('t.assignee_user_id IS NULL')
        elif assignee_user_id is not None:
            clauses.append('t.assignee_user_id=?')
            params.append(self._integer(assignee_user_id, '负责人编号'))
        if requirement_id is not None:
            requirement_id = self._integer(requirement_id, '需求编号')
            self.get_requirement(project_id, requirement_id)
            clauses.append('t.requirement_id=?')
            params.append(requirement_id)
        if keyword.strip():
            # instr treats percent/underscore/quotes as literal text, not SQL wildcards.
            clauses.append("instr(lower(t.title || ' ' || COALESCE(r.title,'')),lower(?))>0")
            params.append(keyword.strip())
        source = '''FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            LEFT JOIN requirements r ON r.id=t.requirement_id AND r.project_id=t.project_id WHERE '''
        source += ' AND '.join(clauses)
        # A savepoint gives count and rows one read snapshot without committing caller work.
        self.db.execute('SAVEPOINT task_query')
        try:
            total = self._one('SELECT COUNT(*) AS n ' + source, params)['n']
            rows = self.db.execute('''SELECT t.id,t.project_id,t.requirement_id,t.title,t.assignee_user_id,
                u.email AS assignee_email,t.due_date,t.status,t.position,t.created_by,t.created_at,t.version '''
                + source + ''' ORDER BY CASE t.status WHEN 'backlog' THEN 0 WHEN 'todo' THEN 1
                    WHEN 'doing' THEN 2 ELSE 3 END,t.position,t.id LIMIT ? OFFSET ?''',
                [*params, page_size, (page - 1) * page_size]).fetchall()
        finally:
            self.db.execute('RELEASE task_query')
        return {'items': [dict(row) for row in rows], 'total': total, 'page': page,
                'page_size': page_size, 'total_pages': (total + page_size - 1) // page_size}

    def _validate_assignee(self, project_id, user_id):
        if isinstance(user_id, bool) or isinstance(user_id, float):
            raise AppError('负责人编号必须为整数。')
        if user_id in (None, '', 0, '0'):
            return None
        user_id = self._integer(user_id, '负责人编号')
        if self.is_owner(project_id, user_id):
            return user_id
        if not self._one('SELECT 1 FROM memberships WHERE project_id=? AND user_id=?',
                         (project_id, user_id)):
            raise AppError('负责人必须是当前项目成员。')
        return user_id

    def create_task(self, project_id, title, requirement_id, assignee_user_id,
                    due_date, created_by, status='backlog'):
        self._project_exists(project_id)
        title = self._short_text(title, '任务标题', 100)
        if status not in TASK_STATUSES:
            raise AppError('任务状态不合法。')
        requirement_id = self._requirement_link(project_id, requirement_id)
        assignee_user_id = self._validate_assignee(project_id, assignee_user_id)
        due_date = self._due_date(due_date)
        self.get_user(created_by)
        position = self._one('''SELECT COALESCE(MAX(position),-1)+1 AS n FROM tasks
            WHERE project_id=? AND status=?''', (project_id, status))['n']
        position = self._integer(position, '任务排序值', minimum=0)
        with self.db:
            cur = self.db.execute('''INSERT INTO tasks(
                project_id,requirement_id,title,assignee_user_id,due_date,status,position,created_by)
                SELECT ?,?,?,?,?,?,?,? WHERE ? IS NULL OR EXISTS (
                    SELECT 1 FROM requirements WHERE id=? AND project_id=? AND archived=0)''',
                (project_id, requirement_id, title, assignee_user_id, due_date,
                 status, position, created_by, requirement_id, requirement_id, project_id))
            if cur.rowcount != 1:
                raise AppError('关联需求已发生变化，请刷新后重试。', 409)
            self._record_event(project_id, 'task', cur.lastrowid, 'create',
                               'status', None, status, created_by)
        return self.get_task(project_id, cur.lastrowid)

    def get_task(self, project_id, task_id):
        row = self._one('''
            SELECT t.id,t.project_id,t.requirement_id,t.title,t.assignee_user_id,
                   u.email AS assignee_email,t.due_date,t.status,t.position,t.created_by,t.created_at,t.version
            FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE t.project_id=? AND t.id=?''', (project_id, task_id))
        if not row:
            raise AppError('任务不存在或不属于当前项目。', 404)
        return dict(row)

    def update_task(self, project_id, task_id, changes, actor=None):
        current = self.get_task(project_id, task_id)
        allowed = {'title', 'requirement_id', 'assignee_user_id', 'due_date', 'status', 'position', 'version'}
        if not isinstance(changes, dict) or not changes or any(key not in allowed for key in changes):
            raise AppError('任务更新字段不合法。')
        if 'version' not in changes:
            raise AppError('更新任务须携带读取时的版本号，请刷新后重试。', 428)
        version = self._integer(changes['version'], '任务版本')
        if version != current['version']:
            raise AppError('任务已被其他操作修改，请刷新后重新编辑。', 409)
        if not (allowed - {'version'}).intersection(changes):
            raise AppError('至少提供一个任务修改字段。')
        values = dict(current)
        values.update(changes)
        values['title'] = self._short_text(values['title'], '任务标题', 100)
        if values['status'] not in TASK_STATUSES:
            raise AppError('任务状态不合法。')
        requirement_id = self._requirement_link(project_id, values['requirement_id'], current['requirement_id'])
        assignee = self._validate_assignee(project_id, values['assignee_user_id'])
        due_date = self._due_date(values['due_date'])
        position = self._integer(values['position'], '任务排序值', minimum=0)
        with self.db:
            if values['status'] != current['status'] and 'position' not in changes:
                position = self._one('''SELECT COALESCE(MAX(position),-1)+1 AS n FROM tasks
                    WHERE project_id=? AND status=?''', (project_id, values['status']))['n']
                position = self._integer(position, '任务排序值', minimum=0)
            result = self.db.execute('''UPDATE tasks SET title=?,requirement_id=?,assignee_user_id=?,
                due_date=?,status=?,position=?,version=version+1 WHERE project_id=? AND id=? AND version=?
                AND (? IS NULL OR requirement_id=? OR EXISTS (
                    SELECT 1 FROM requirements WHERE id=? AND project_id=? AND archived=0))''',
                (values['title'], requirement_id, assignee, due_date, values['status'],
                 position, project_id, task_id, version, requirement_id, requirement_id, requirement_id, project_id))
            if result.rowcount != 1:
                raise AppError('任务已被其他操作修改，请刷新后重新编辑。', 409)
            self._record_task_changes(current, {
                'project_id': project_id, 'id': task_id, 'title': values['title'],
                'requirement_id': requirement_id, 'assignee_user_id': assignee,
                'due_date': due_date, 'status': values['status'], 'position': position}, actor)
        return self.get_task(project_id, task_id)

    # ---------- Sprint 3 项目统计、风险清单与下钻 ----------
    def _project_tasks(self, project_id):
        return self.db.execute('''SELECT t.id,t.title,t.status,t.due_date,t.assignee_user_id,
                u.email AS assignee_email,t.requirement_id
            FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE t.project_id=? ORDER BY t.id''', (project_id,)).fetchall()

    def project_stats(self, project_id, days=14, today=None):
        """汇总当前项目的任务健康度；数字口径见 README「统计口径」。"""
        project_id = self._integer(project_id, '项目编号')
        self._project_exists(project_id)
        days = self._integer(days, '统计天数')
        if days > 90:
            raise AppError('统计天数不得超过 90 天。')
        today = self._stats_today(today)
        today_text = today.isoformat()
        rows = self._project_tasks(project_id)
        total = len(rows)
        completed = sum(1 for row in rows if row['status'] == 'done')
        overdue = sum(1 for row in rows if row['status'] != 'done' and row['due_date']
                      and row['due_date'] < today_text)
        due_soon = sum(1 for row in rows if row['status'] != 'done' and row['due_date']
                       and today_text <= row['due_date'] <= (today + timedelta(days=3)).isoformat())
        by_status = [{'status': status,
                      'count': sum(1 for row in rows if row['status'] == status)}
                     for status in TASK_STATUSES]
        by_assignee = [{'user_id': row['user_id'], 'email': row['email'] or '未指派',
                        'total': row['total'], 'completed': row['completed']}
                       for row in self.db.execute('''SELECT t.assignee_user_id AS user_id,
                            u.email AS email,COUNT(*) AS total,
                            SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS completed
                        FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
                        WHERE t.project_id=? GROUP BY t.assignee_user_id
                        ORDER BY total DESC,u.email''', (project_id,))]
        by_requirement = [{'requirement_id': row['id'], 'title': row['title'],
                           'archived': bool(row['archived']), 'total': row['total'],
                           'completed': row['completed']}
                          for row in self.db.execute('''SELECT r.id,r.title,r.archived,
                                COUNT(t.id) AS total,
                                SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS completed
                            FROM requirements r JOIN tasks t
                              ON t.requirement_id=r.id AND t.project_id=r.project_id
                            WHERE r.project_id=? GROUP BY r.id ORDER BY r.id''', (project_id,))]
        unlinked = self.db.execute('''SELECT COUNT(*) AS total,
                SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed
            FROM tasks WHERE project_id=? AND requirement_id IS NULL''', (project_id,)).fetchone()
        if unlinked['total']:
            by_requirement.append({'requirement_id': None, 'title': '未关联需求', 'archived': False,
                                   'total': unlinked['total'], 'completed': unlinked['completed'] or 0})
        start = today - timedelta(days=days - 1)
        done_days = {row['entity_id']: row['day'] for row in self.db.execute('''
            SELECT entity_id, MIN(substr(created_at,1,10)) AS day FROM audit_events
            WHERE project_id=? AND entity_type='task' AND field='status' AND to_value='done'
            GROUP BY entity_id''', (project_id,))}
        baseline, per_day = 0, {}
        for row in rows:
            if row['status'] != 'done':
                continue
            day = done_days.get(row['id'])
            if day is None or day < start.isoformat():
                baseline += 1
            else:
                per_day[day] = per_day.get(day, 0) + 1
        trend, running = [], baseline
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            running += per_day.get(day, 0)
            trend.append({'date': day, 'completed': running})
        return {
            'as_of': today_text,
            'window_days': days,
            'total': total,
            'completed': completed,
            'overdue': overdue,
            'due_soon': due_soon,
            'completion_rate': round(completed * 100.0 / total, 1) if total else 0.0,
            'by_status': by_status,
            'by_assignee': by_assignee,
            'by_requirement': by_requirement,
            'trend': trend,
        }

    @staticmethod
    def _stats_today(today):
        if today is None:
            return date.today()
        if isinstance(today, date):
            return today
        if isinstance(today, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', today):
            try:
                return date.fromisoformat(today)
            except ValueError:
                pass
        raise AppError('统计基准日必须为 YYYY-MM-DD。')

    def project_risks(self, project_id, limit=10, today=None):
        """逾期与临期任务清单（US-38），按截止日升序。"""
        project_id = self._integer(project_id, '项目编号')
        self._project_exists(project_id)
        limit = self._integer(limit, '返回条数')
        if limit > 50:
            raise AppError('风险清单单次最多返回 50 条。')
        today = self._stats_today(today)
        rows = self.db.execute('''SELECT t.id,t.title,t.status,t.due_date,t.assignee_user_id,
                u.email AS assignee_email FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE t.project_id=? AND t.status<>'done' AND t.due_date IS NOT NULL
            ORDER BY t.due_date,t.id LIMIT ?''', (project_id, limit)).fetchall()
        items = []
        for row in rows:
            days_left = (date.fromisoformat(row['due_date']) - today).days
            if days_left > 3:
                continue
            items.append({**dict(row), 'days_left': days_left,
                          'level': 'overdue' if days_left < 0 else 'due_soon'})
        return {'as_of': today.isoformat(), 'items': items,
                'overdue': sum(1 for item in items if item['level'] == 'overdue'),
                'due_soon': sum(1 for item in items if item['level'] == 'due_soon')}

    def drilldown_tasks(self, project_id, dimension, value, limit=100):
        """从统计图下钻到任务明细（US-40），只返回当前项目数据。"""
        project_id = self._integer(project_id, '项目编号')
        self._project_exists(project_id)
        limit = self._integer(limit, '返回条数')
        if limit > 200:
            raise AppError('下钻明细单次最多返回 200 条。')
        clauses, params = ['t.project_id=?'], [project_id]
        if dimension == 'status':
            if value not in TASK_STATUSES:
                raise AppError('下钻维度取值不合法。')
            clauses.append('t.status=?')
            params.append(value)
        elif dimension == 'assignee':
            if value in (None, '', 'unassigned'):
                clauses.append('t.assignee_user_id IS NULL')
            else:
                clauses.append('t.assignee_user_id=?')
                params.append(self._integer(value, '负责人编号'))
        elif dimension == 'requirement':
            if value in (None, '', 'none'):
                clauses.append('t.requirement_id IS NULL')
            else:
                clauses.append('t.requirement_id=?')
                params.append(self._integer(value, '需求编号'))
        elif dimension == 'completed_on':
            day = self._stats_today(value).isoformat()
            clauses.append('''t.status='done' AND t.id IN (SELECT entity_id FROM audit_events
                WHERE project_id=? AND entity_type='task' AND field='status' AND to_value='done'
                GROUP BY entity_id HAVING MIN(substr(created_at,1,10))=?)''')
            params.extend([project_id, day])
        elif dimension == 'overdue':
            today = self._stats_today(value if value not in (None, '') else None)
            clauses.append("t.status<>'done' AND t.due_date IS NOT NULL AND t.due_date<?")
            params.append(today.isoformat())
        elif dimension == 'due_soon':
            today = self._stats_today(value if value not in (None, '') else None)
            clauses.append("t.status<>'done' AND t.due_date IS NOT NULL "
                           "AND t.due_date>=? AND t.due_date<=?")
            params.extend([today.isoformat(), (today + timedelta(days=3)).isoformat()])
        else:
            raise AppError('下钻维度不合法。')
        rows = self.db.execute('''SELECT t.id,t.title,t.status,t.due_date,t.assignee_user_id,
                u.email AS assignee_email,t.requirement_id
            FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE ''' + ' AND '.join(clauses) + ' ORDER BY t.status,t.due_date,t.id LIMIT ?',
            [*params, limit]).fetchall()
        return {'dimension': dimension, 'value': value,
                'items': [dict(row) for row in rows], 'total': len(rows),
                'truncated': len(rows) == limit}
