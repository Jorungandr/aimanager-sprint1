"""Sprint 1 权限基线与 Sprint 2 协作增量。查询统一使用参数绑定。"""
from datetime import date
from contextlib import closing
from pathlib import Path
import hashlib
import hmac
import os
import re
import sqlite3

PERMISSIONS = ('project.view', 'members.manage', 'projects.manage', 'roles.manage',
               'workitems.manage')
TASK_STATUSES = ('backlog', 'todo', 'doing', 'done')
PROJECT_PERMISSIONS = {'project.view', 'members.manage', 'workitems.manage'}


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
          CREATE TABLE IF NOT EXISTS role_permissions (
            role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            permission TEXT NOT NULL, PRIMARY KEY(role_id, permission));
          CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS memberships (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role_id INTEGER NOT NULL REFERENCES roles(id), PRIMARY KEY(project_id, user_id));
          CREATE TABLE IF NOT EXISTS requirements (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            version INTEGER NOT NULL DEFAULT 1,
            archived_at TEXT);
          CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            requirement_id INTEGER REFERENCES requirements(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            assignee_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'backlog'
              CHECK(status IN ('backlog','todo','doing','done')),
            position INTEGER NOT NULL DEFAULT 0,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')
        # Back up the pre-migration database once, using SQLite's consistent snapshot.
        task_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(tasks)')}
        project_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(projects)')}
        if 'version' not in task_columns or 'owner_user_id' not in project_columns:
            if path != ':memory:' and self._one('SELECT COUNT(*) AS n FROM users')['n']:
                backup = Path(str(path) + '.pre-hardening.bak')
                if not backup.exists():
                    with closing(sqlite3.connect(str(backup))) as snapshot:
                        self.db.backup(snapshot)
            with self.db:
                if 'version' not in task_columns:
                    self.db.execute('ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
                if 'owner_user_id' not in project_columns:
                    self.db.execute('ALTER TABLE projects ADD COLUMN owner_user_id INTEGER REFERENCES users(id)')
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO role_permissions(role_id,permission)
                SELECT DISTINCT role_id,'project.view' FROM role_permissions WHERE permission IN
                ('members.manage','workitems.manage','roles.manage','projects.manage')''')

    def close(self):
        self.db.close()

    def _one(self, sql, params=()):
        return self.db.execute(sql, params).fetchone()

    def needs_setup(self):
        return self._one('SELECT COUNT(*) AS n FROM users')['n'] == 0

    @staticmethod
    def _hash(password, salt=None):
        salt = salt or os.urandom(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 150_000)
        return f'{salt.hex()}:{digest.hex()}'

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
            raise AppError('邮箱或密码错误。', 401)
        salt, expected = row['password_hash'].split(':')
        actual = self._hash(password, bytes.fromhex(salt)).split(':')[1]
        if not hmac.compare_digest(actual, expected):
            raise AppError('邮箱或密码错误。', 401)
        return self.get_user(row['id'])

    def get_user(self, user_id):
        row = self._one('SELECT id,email,is_admin FROM users WHERE id=?', (user_id,))
        if not row:
            raise AppError('用户不存在。', 404)
        return {'id': row['id'], 'email': row['email'], 'is_admin': bool(row['is_admin'])}

    def list_users(self):
        return [dict(row) for row in self.db.execute('SELECT id,email FROM users ORDER BY id')]

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
        roles = []
        for row in self.db.execute('SELECT id,name FROM roles ORDER BY id'):
            permissions = [x['permission'] for x in self.db.execute(
                'SELECT permission FROM role_permissions WHERE role_id=? ORDER BY permission', (row['id'],))]
            roles.append({'id': row['id'], 'name': row['name'], 'permissions': permissions})
        return roles

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
        with self.db:
            self.db.execute('''INSERT INTO memberships(project_id,user_id,role_id) VALUES(?,?,?)
                ON CONFLICT(project_id,user_id) DO UPDATE SET role_id=excluded.role_id''',
                (project_id, user_id, role_id))

    def list_members(self, project_id):
        return [dict(row) for row in self.db.execute('''SELECT u.id AS user_id,u.email,r.name AS role_name,0 AS is_owner
            FROM memberships m JOIN users u ON u.id=m.user_id JOIN roles r ON r.id=m.role_id
            WHERE m.project_id=? AND NOT EXISTS(SELECT 1 FROM projects p WHERE p.id=m.project_id AND p.owner_user_id=m.user_id)
            UNION ALL SELECT u.id,u.email,'项目创建者',1 FROM projects p JOIN users u ON u.id=p.owner_user_id
            WHERE p.id=? ORDER BY email''', (project_id, project_id))]

    def remove_member(self, project_id, user_id, actor=None):
        if self.is_owner(project_id, user_id):
            raise AppError('不能移除项目创建者，请保留项目管理入口。', 409)
        if actor is not None:
            allowed = {role['id'] for role in self.assignable_roles(actor, project_id)}
            current = self._one('SELECT role_id FROM memberships WHERE project_id=? AND user_id=?', (project_id, user_id))
            if current and current['role_id'] not in allowed:
                raise AppError('不能移除高于本人授权范围的成员。', 403)
        with self.db:
            cur = self.db.execute('DELETE FROM memberships WHERE project_id=? AND user_id=?',
                                  (project_id, user_id))
            # Preserve work items but release assignments when project access is revoked.
            if cur.rowcount:
                self.db.execute('UPDATE tasks SET assignee_user_id=NULL,version=version+1 WHERE project_id=? AND assignee_user_id=?',
                                (project_id, user_id))
        if not cur.rowcount:
            raise AppError('成员关系不存在。', 404)

    def get_requirement(self, project_id, requirement_id):
        row = self._one('''SELECT id,project_id,title,description,created_by,created_at,version,archived_at
            FROM requirements WHERE project_id=? AND id=?''', (project_id, requirement_id))
        if not row:
            raise AppError('需求不存在或不属于当前项目。', 404)
        return dict(row)

    def update_requirement(self, project_id, requirement_id, changes):
        current = self.get_requirement(project_id, requirement_id)
        allowed = {'title', 'description', 'version'}
        if not isinstance(changes, dict) or not changes or any(key not in allowed for key in changes):
            raise AppError('需求更新字段不合法。')
        if 'version' not in changes:
            raise AppError('更新需求须携带读取时的版本号，请刷新后重试。', 428)
        version = self._integer(changes['version'], '需求版本')
        if version != current['version']:
            raise AppError('需求已被其他操作修改，请刷新后重新编辑。', 409)
        title = self._short_text(changes.get('title', current['title']), '需求标题', 80)
        description = str(changes.get('description', current['description'])).strip()
        if len(description) > 500:
            raise AppError('需求说明不得超过 500 字。')
        with self.db:
            result = self.db.execute('UPDATE requirements SET title=?,description=?,version=version+1 WHERE project_id=? AND id=? AND version=?',
                            (title, description, project_id, requirement_id, version))
            if result.rowcount != 1:
                raise AppError('需求已被其他操作修改，请刷新后重新编辑。', 409)
        return self.get_requirement(project_id, requirement_id)

    def archive_requirement(self, project_id, requirement_id, version):
        """归档需求，不永久删除。历史任务保留需求关联。"""
        current = self.get_requirement(project_id, requirement_id)
        if current['archived_at']:
            raise AppError('需求已归档。', 400)
        if version != current['version']:
            raise AppError('需求已被其他操作修改，请刷新后重新操作。', 409)
        from datetime import datetime
        archived_at = datetime.now().isoformat()
        with self.db:
            result = self.db.execute('UPDATE requirements SET archived_at=?,version=version+1 WHERE project_id=? AND id=? AND version=?',
                            (archived_at, project_id, requirement_id, version))
            if result.rowcount != 1:
                raise AppError('需求已被其他操作修改，请刷新后重新操作。', 409)
        return self.get_requirement(project_id, requirement_id)

    def restore_requirement(self, project_id, requirement_id, version):
        """恢复已归档的需求。"""
        current = self.get_requirement(project_id, requirement_id)
        if not current['archived_at']:
            raise AppError('需求未归档。', 400)
        if version != current['version']:
            raise AppError('需求已被其他操作修改，请刷新后重新操作。', 409)
        with self.db:
            result = self.db.execute('UPDATE requirements SET archived_at=NULL,version=version+1 WHERE project_id=? AND id=? AND version=?',
                            (project_id, requirement_id, version))
            if result.rowcount != 1:
                raise AppError('需求已被其他操作修改，请刷新后重新操作。', 409)
        return self.get_requirement(project_id, requirement_id)

    def delete_task(self, project_id, task_id):
        self.get_task(project_id, task_id)
        with self.db:
            self.db.execute('DELETE FROM tasks WHERE project_id=? AND id=?', (project_id, task_id))

    def update_role_name(self, role_id, name):
        name = str(name).strip()
        if not name or len(name) > 30:
            raise AppError('角色名称应为 1 至 30 字。')
        if not self._one('SELECT id FROM roles WHERE id=?', (role_id,)):
            raise AppError('角色不存在。', 404)
        try:
            with self.db:
                self.db.execute('UPDATE roles SET name=? WHERE id=?', (name, role_id))
        except sqlite3.IntegrityError as exc:
            raise AppError('角色名称已存在。') from exc

    def delete_role(self, role_id):
        if not self._one('SELECT id FROM roles WHERE id=?', (role_id,)):
            raise AppError('角色不存在。', 404)
        with self.db:
            self.db.execute('DELETE FROM roles WHERE id=?', (role_id,))

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
    def list_requirements(self, project_id, include_archived=False):
        self._project_exists(project_id)
        if include_archived:
            return [dict(row) for row in self.db.execute('''
                SELECT id,project_id,title,description,created_by,created_at,version,archived_at
                FROM requirements WHERE project_id=? ORDER BY archived_at IS NULL DESC, id DESC''', (project_id,))]
        else:
            return [dict(row) for row in self.db.execute('''
                SELECT id,project_id,title,description,created_by,created_at,version,archived_at
                FROM requirements WHERE project_id=? AND archived_at IS NULL ORDER BY id DESC''', (project_id,))]

    def create_requirement(self, project_id, title, description, created_by):
        self._project_exists(project_id)
        title = self._short_text(title, '需求标题', 80)
        if not isinstance(description, str):
            raise AppError('需求说明必须是文本。')
        description = description.strip()
        if len(description) > 500:
            raise AppError('需求说明不得超过 500 字。')
        self.get_user(created_by)
        with self.db:
            cur = self.db.execute('''INSERT INTO requirements(project_id,title,description,created_by)
                VALUES(?,?,?,?)''', (project_id, title, description, created_by))
        return self.get_requirement(project_id, cur.lastrowid)

    def list_tasks(self, project_id):
        self._project_exists(project_id)
        return [dict(row) for row in self.db.execute('''
            SELECT t.id,t.project_id,t.requirement_id,t.title,t.assignee_user_id,
                   u.email AS assignee_email,t.due_date,t.status,t.position,t.created_by,t.created_at,t.version
            FROM tasks t LEFT JOIN users u ON u.id=t.assignee_user_id
            WHERE t.project_id=?
            ORDER BY CASE t.status WHEN 'backlog' THEN 0 WHEN 'todo' THEN 1
                      WHEN 'doing' THEN 2 ELSE 3 END, t.position, t.id''', (project_id,))]

    def query_tasks(self, project_id, page=1, page_size=20, status=None, assignee=None, search=None):
        """分页查询任务，支持筛选"""
        self._project_exists(project_id)
        conditions = ['t.project_id=?']
        params = [project_id]
        if status:
            conditions.append('t.status=?')
            params.append(status)
        if assignee:
            if assignee == 'unassigned':
                conditions.append('t.assignee_user_id IS NULL')
            else:
                conditions.append('t.assignee_user_id=?')
                params.append(int(assignee))
        if search:
            conditions.append('(t.title LIKE ? OR r.title LIKE ?)')
            params.extend([f'%{search}%', f'%{search}%'])
        where_clause = ' AND '.join(conditions)
        # 计算总数
        count_sql = f'''SELECT COUNT(*) as total FROM tasks t
                       LEFT JOIN requirements r ON r.id=t.requirement_id
                       WHERE {where_clause}'''
        total = self._one(count_sql, params)['total']
        # 查询分页数据
        offset = (page - 1) * page_size
        data_sql = f'''SELECT t.id,t.project_id,t.requirement_id,t.title,t.assignee_user_id,
                       u.email AS assignee_email,t.due_date,t.status,t.position,t.created_by,t.created_at,t.version
                FROM tasks t
                LEFT JOIN users u ON u.id=t.assignee_user_id
                LEFT JOIN requirements r ON r.id=t.requirement_id
                WHERE {where_clause}
                ORDER BY CASE t.status WHEN 'backlog' THEN 0 WHEN 'todo' THEN 1
                          WHEN 'doing' THEN 2 ELSE 3 END, t.position, t.id
                LIMIT ? OFFSET ?'''
        params.extend([page_size, offset])
        items = [dict(row) for row in self.db.execute(data_sql, params)]
        return {
            'items': items,
            'total': total,
            'page': page,
            'pageSize': page_size,
            'totalPages': (total + page_size - 1) // page_size
        }

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
        if isinstance(requirement_id, bool) or isinstance(requirement_id, float):
            raise AppError('需求编号必须为整数。')
        if requirement_id in (None, '', 0, '0'):
            requirement_id = None
        else:
            requirement_id = self._integer(requirement_id, '需求编号')
            self.get_requirement(project_id, requirement_id)
        assignee_user_id = self._validate_assignee(project_id, assignee_user_id)
        due_date = self._due_date(due_date)
        self.get_user(created_by)
        position = self._one('''SELECT COALESCE(MAX(position),-1)+1 AS n FROM tasks
            WHERE project_id=? AND status=?''', (project_id, status))['n']
        with self.db:
            cur = self.db.execute('''INSERT INTO tasks(
                project_id,requirement_id,title,assignee_user_id,due_date,status,position,created_by)
                VALUES(?,?,?,?,?,?,?,?)''',
                (project_id, requirement_id, title, assignee_user_id, due_date,
                 status, position, created_by))
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

    def update_task(self, project_id, task_id, changes):
        current = self.get_task(project_id, task_id)
        allowed = {'title', 'requirement_id', 'assignee_user_id', 'due_date', 'status', 'position', 'version'}
        if not isinstance(changes, dict) or not changes or any(key not in allowed for key in changes):
            raise AppError('任务更新字段不合法。')
        if 'version' not in changes:
            raise AppError('更新任务须携带读取时的版本号，请刷新后重试。', 428)
        version = self._integer(changes['version'], '任务版本')
        if version != current['version']:
            raise AppError('任务已被其他操作修改，请刷新后重新编辑。', 409)
        values = dict(current)
        values.update(changes)
        values['title'] = self._short_text(values['title'], '任务标题', 100)
        if values['status'] not in TASK_STATUSES:
            raise AppError('任务状态不合法。')
        requirement_id = values['requirement_id']
        if isinstance(requirement_id, bool) or isinstance(requirement_id, float):
            raise AppError('需求编号必须为整数。')
        if requirement_id in (None, '', 0, '0'):
            requirement_id = None
        else:
            requirement_id = self._integer(requirement_id, '需求编号')
            self.get_requirement(project_id, requirement_id)
        assignee = self._validate_assignee(project_id, values['assignee_user_id'])
        due_date = self._due_date(values['due_date'])
        position = self._integer(values['position'], '任务排序值', minimum=0)
        with self.db:
            result = self.db.execute('''UPDATE tasks SET title=?,requirement_id=?,assignee_user_id=?,
                due_date=?,status=?,position=?,version=version+1 WHERE project_id=? AND id=? AND version=?''',
                (values['title'], requirement_id, assignee, due_date, values['status'],
                 position, project_id, task_id, version))
            if result.rowcount != 1:
                raise AppError('任务已被其他操作修改，请刷新后重新编辑。', 409)
        return self.get_task(project_id, task_id)
