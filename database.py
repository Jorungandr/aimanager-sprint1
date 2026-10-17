"""SQLite connection setup, schema migration and pre-upgrade backups."""
from contextlib import closing
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 2


class DatabaseVersionError(Exception):
    """The database was created by a newer, unsupported application."""


def open_database(path):
    """Return an initialized SQLite connection; close it on any setup failure."""
    db = sqlite3.connect(path, check_same_thread=False)
    try:
        _initialize(db, path)
    except Exception:
        db.close()
        raise
    return db


def _initialize(db, path):
    db.row_factory = sqlite3.Row
    # Refuse newer schemas before executing any DDL or data normalization.
    if db.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
        raise DatabaseVersionError('数据库版本高于当前程序支持范围，请使用更新版本的程序。')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
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
        version INTEGER NOT NULL DEFAULT 1,
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
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
    task_columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
    project_columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
    if 'version' not in task_columns or 'owner_user_id' not in project_columns:
        if path != ':memory:' and db.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']:
            backup = Path(str(path) + '.pre-hardening.bak')
            if not backup.exists():
                with closing(sqlite3.connect(str(backup))) as snapshot:
                    db.backup(snapshot)
        with db:
            if 'version' not in task_columns:
                db.execute('ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
            if 'owner_user_id' not in project_columns:
                db.execute('ALTER TABLE projects ADD COLUMN owner_user_id INTEGER REFERENCES users(id)')
    with db:
        db.execute('''INSERT OR IGNORE INTO role_permissions(role_id,permission)
            SELECT DISTINCT role_id,'project.view' FROM role_permissions WHERE permission IN
            ('members.manage','workitems.manage','roles.manage','projects.manage')''')
    requirement_columns = {row['name'] for row in db.execute('PRAGMA table_info(requirements)')}
    if not {'version', 'archived'}.issubset(requirement_columns):
        if str(path) != ':memory:' and db.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']:
            backup = Path(str(path) + '.pre-requirements.bak')
            if not backup.exists():
                with closing(sqlite3.connect(str(backup))) as snapshot:
                    db.backup(snapshot)
        with db:
            db.execute('BEGIN IMMEDIATE')
            if 'version' not in requirement_columns:
                db.execute('ALTER TABLE requirements ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
            if 'archived' not in requirement_columns:
                db.execute('ALTER TABLE requirements ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))')
    with db:
        db.execute('CREATE INDEX IF NOT EXISTS idx_requirements_project_archived ON requirements(project_id,archived,id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_status_position ON tasks(project_id,status,position,id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_assignee ON tasks(project_id,assignee_user_id)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id,project_id,role_id)')
        # v1: task versions and project owners; v2: requirement versions/archive.
        # Column checks above also repair legacy schemas that have no version marker.
        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
