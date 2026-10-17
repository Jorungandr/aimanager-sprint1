"""Read-only diagnostics. Exit 0: healthy, 1: findings, 2: unable to inspect.

Run: python scripts/check_database.py PATH_TO_DATABASE
Stop writers first for a reproducible delivery check. Never repairs or migrates.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import SCHEMA_VERSION


def inspect_database(path):
    target = Path(path).resolve(strict=True)
    if not target.is_file():
        raise ValueError('Database path must be an existing file')
    findings = []
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            return {'schema_version': version, 'findings': [{'category': 'schema_version', 'count': 1}]}
        # Restrict output to fixed labels and identifiers, never integrity error text.
        errors = sum(row[0] != 'ok' for row in db.execute('PRAGMA integrity_check'))
        if errors:
            findings.append({'category': 'integrity', 'count': errors})
            return {'schema_version': version, 'findings': findings}
        required = {
            'users': {'id', 'email', 'password_hash', 'is_admin'},
            'roles': {'id', 'name'},
            'role_permissions': {'role_id', 'permission'},
            'projects': {'id', 'name', 'owner_user_id'},
            'memberships': {'project_id', 'user_id', 'role_id'},
            'requirements': {'id', 'project_id', 'title', 'description', 'version', 'archived', 'created_by', 'created_at'},
            'tasks': {'id', 'project_id', 'requirement_id', 'assignee_user_id', 'title', 'status',
                      'version', 'due_date', 'position', 'created_by', 'created_at'},
        }
        for table, columns in required.items():
            actual = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if not columns.issubset(actual):
                findings.append({'category': 'schema_columns', 'table': table,
                                 'count': len(columns - actual)})
        if findings:
            return {'schema_version': version, 'findings': findings}
        # Foreign-key diagnostics may contain attacker-controlled table names.
        violations = list(db.execute('PRAGMA foreign_key_check'))
        if violations:
            findings.append({'category': 'foreign_keys', 'count': len(violations),
                             'record_ids': [row[1] for row in violations[:100]]})
        checks = {
            'cross_project_requirement': '''SELECT t.id FROM tasks t JOIN requirements r
                ON r.id=t.requirement_id WHERE t.project_id<>r.project_id ORDER BY t.id''',
            'invalid_assignee': '''SELECT t.id FROM tasks t WHERE t.assignee_user_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=t.project_id
                    AND p.owner_user_id=t.assignee_user_id)
                AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.project_id=t.project_id
                    AND m.user_id=t.assignee_user_id) ORDER BY t.id''',
        }
        for category, sql in checks.items():
            ids = [row[0] for row in db.execute(sql)]
            if ids:
                findings.append({'category': category, 'count': len(ids), 'record_ids': ids[:100]})
        return {'schema_version': version, 'findings': findings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', help='Existing SQLite file; no default production path')
    args = parser.parse_args(argv)
    try:
        report = inspect_database(args.database)
    except (OSError, ValueError, sqlite3.Error):
        # Do not print SQLite exceptions: malformed schemas can embed sensitive text.
        print(json.dumps({'error': 'Database inspection failed; verify path, access and SQLite format.'}))
        return 2
    print(json.dumps(report, ensure_ascii=True))
    return 1 if report['findings'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
