"""E01 数据与权限规则。数据库查询统一使用参数绑定。"""
import hashlib
import hmac
import os
import re
import sqlite3

PERMISSIONS = ('project.view', 'members.manage', 'projects.manage', 'roles.manage')


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
        ''')

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
        email = str(email).strip().lower()
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
        row = self._one('SELECT * FROM users WHERE email=?', (str(email).strip().lower(),))
        if not row or not isinstance(password, str):
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
        return self._one('''SELECT 1 FROM memberships m JOIN role_permissions rp ON rp.role_id=m.role_id
            WHERE m.user_id=? AND rp.permission=?''', (user['id'], permission)) is not None

    def capabilities(self, user):
        return {
            'can_manage_roles': self.has_global_permission(user, 'roles.manage'),
            'can_manage_projects': self.has_global_permission(user, 'projects.manage'),
            'can_manage_members': self.has_global_permission(user, 'members.manage'),
        }

    def create_role(self, name):
        name = str(name).strip()
        if not name or len(name) > 30:
            raise AppError('角色名称应为 1 至 30 字。')
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

    def set_permissions(self, role_id, permissions):
        if not self._one('SELECT id FROM roles WHERE id=?', (role_id,)):
            raise AppError('角色不存在。', 404)
        if not isinstance(permissions, list) or any(p not in PERMISSIONS for p in permissions):
            raise AppError('权限项不合法。')
        with self.db:
            self.db.execute('DELETE FROM role_permissions WHERE role_id=?', (role_id,))
            self.db.executemany('INSERT INTO role_permissions(role_id,permission) VALUES(?,?)',
                                [(role_id, p) for p in set(permissions)])

    def create_project(self, name):
        name = str(name).strip()
        if not name or len(name) > 60:
            raise AppError('项目名称应为 1 至 60 字。')
        with self.db:
            cur = self.db.execute('INSERT INTO projects(name) VALUES(?)', (name,))
        return {'id': cur.lastrowid, 'name': name}

    def has_permission(self, user, project_id, permission):
        if user['is_admin']:
            return self._one('SELECT id FROM projects WHERE id=?', (project_id,)) is not None
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
            rows = self.db.execute('''SELECT p.id,p.name FROM projects p JOIN memberships m ON m.project_id=p.id
                JOIN role_permissions rp ON rp.role_id=m.role_id WHERE m.user_id=? AND rp.permission='project.view'
                ORDER BY p.id''', (user['id'],))
        return [dict(row) for row in rows]

    def add_member(self, project_id, user_id, role_id):
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
        return [dict(row) for row in self.db.execute('''SELECT u.id AS user_id,u.email,r.name AS role_name
            FROM memberships m JOIN users u ON u.id=m.user_id JOIN roles r ON r.id=m.role_id
            WHERE m.project_id=? ORDER BY u.email''', (project_id,))]

    def remove_member(self, project_id, user_id):
        with self.db:
            cur = self.db.execute('DELETE FROM memberships WHERE project_id=? AND user_id=?',
                                  (project_id, user_id))
        if not cur.rowcount:
            raise AppError('成员关系不存在。', 404)
