"""SQLite connection setup, schema migration and pre-upgrade backups."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile

SCHEMA_VERSION = 3


class DatabaseVersionError(Exception):
    """The database was created by a newer, unsupported application."""


def open_database(path):
    """Return an initialized SQLite connection; close it on any setup failure."""
    db = sqlite3.connect(path, check_same_thread=False)
    try:
        with db:
            _initialize(db, path)
    except Exception:
        db.close()
        raise
    return db


def _initialize(db, path):
    db.row_factory = sqlite3.Row
    # Refuse newer schemas before executing any DDL or data normalization.
    current_version = db.execute('PRAGMA user_version').fetchone()[0]
    if current_version > SCHEMA_VERSION:
        raise DatabaseVersionError('数据库版本高于当前程序支持范围，请使用更新版本的程序。')
    db.execute('PRAGMA foreign_keys=ON')
    task_columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
    project_columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
    requirement_columns = {row['name'] for row in db.execute('PRAGMA table_info(requirements)')}
    has_users = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if str(path) != ':memory:' and has_users and db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
        if 'version' not in task_columns or 'owner_user_id' not in project_columns:
            _backup(db, path, '.pre-hardening.bak')
        if not {'version', 'archived'}.issubset(requirement_columns):
            _backup(db, path, '.pre-requirements.bak')
        # v3 adds the audit trail; snapshot populated v2 databases before DDL.
        if current_version < 3:
            _backup(db, path, '.pre-audit.bak')
    # executescript commits any existing transaction: begin inside the script.
    # The caller commits all schema/data changes together or rolls them back.
    db.executescript('''
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
      CREATE TABLE IF NOT EXISTS role_permissions (
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        permission TEXT NOT NULL, PRIMARY KEY(role_id, permission));
      CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        owner_user_id INTEGER REFERENCES users(id));
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
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
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
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        version INTEGER NOT NULL DEFAULT 1);
      CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        entity_type TEXT NOT NULL CHECK(entity_type IN ('task','requirement','membership')),
        entity_id INTEGER NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('create','update','archive','restore','remove')),
        field TEXT,
        from_value TEXT,
        to_value TEXT,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
    ''')
    # Re-read columns because the script may have created new tables.
    task_columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
    project_columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
    if 'version' not in task_columns or 'owner_user_id' not in project_columns:
        if 'version' not in task_columns:
            db.execute('ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        if 'owner_user_id' not in project_columns:
            db.execute('ALTER TABLE projects ADD COLUMN owner_user_id INTEGER REFERENCES users(id)')
    db.execute('''INSERT OR IGNORE INTO role_permissions(role_id,permission)
        SELECT DISTINCT role_id,'project.view' FROM role_permissions WHERE permission IN
        ('members.manage','workitems.manage','roles.manage','projects.manage')''')
    requirement_columns = {row['name'] for row in db.execute('PRAGMA table_info(requirements)')}
    if not {'version', 'archived'}.issubset(requirement_columns):
        if 'version' not in requirement_columns:
            db.execute('ALTER TABLE requirements ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        if 'archived' not in requirement_columns:
            db.execute('ALTER TABLE requirements ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_requirements_project_archived ON requirements(project_id,archived,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_status_position ON tasks(project_id,status,position,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_assignee ON tasks(project_id,assignee_user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id,project_id,role_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_project_time ON audit_events(project_id,created_at,id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type,entity_id,id)')
    # v1: task versions and project owners; v2: requirement versions/archive; v3: audit trail.
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')


def _backup(db, path, suffix):
    backup = Path(str(path) + suffix)
    if backup.exists():
        if not backup.is_file():
            raise OSError(f'Backup path is not a file: {backup}')
        return
    # Publish only a complete snapshot; a failed backup must not block retries.
    with tempfile.NamedTemporaryFile(dir=backup.parent, prefix=backup.name + '.', suffix='.tmp', delete=False) as pending:
        temporary = Path(pending.name)
    try:
        with closing(sqlite3.connect(str(temporary))) as snapshot:
            db.backup(snapshot)
        temporary.replace(backup)
    finally:
        temporary.unlink(missing_ok=True)
