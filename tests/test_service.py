"""用户、角色、项目及权限回收的关键路径。"""
import sys
from pathlib import Path
import tempfile
import unittest
import sqlite3
import hashlib
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import AppError, Store, PASSWORD_ITERATIONS, MAX_PASSWORD_ITERATIONS


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / 'test.db'))
        self.admin = self.store.setup('admin@example.test', 'AdminPass123')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_password_is_hashed_and_login_checks_secret(self):
        row = self.store.db.execute('SELECT password_hash FROM users').fetchone()
        self.assertNotIn('AdminPass123', row['password_hash'])
        self.assertEqual(self.store.authenticate('admin@example.test', 'AdminPass123')['id'], self.admin['id'])
        with self.assertRaises(AppError): self.store.authenticate('admin@example.test', 'wrong-pass')

    def test_new_password_hash_records_algorithm_iterations_and_random_salt(self):
        first = self.store.db.execute('SELECT password_hash FROM users').fetchone()[0]
        self.store.create_user('same@example.test', 'AdminPass123')
        second = self.store.db.execute('SELECT password_hash FROM users ORDER BY id DESC').fetchone()[0]
        algorithm, iterations, salt, digest = first.split('$')
        self.assertEqual((algorithm, int(iterations)), ('pbkdf2_sha256', PASSWORD_ITERATIONS))
        self.assertEqual((len(salt), len(digest)), (32, 64))
        self.assertNotEqual(first, second)

    def test_legacy_password_upgrades_only_after_success_and_survives_reopen(self):
        salt = bytes(range(16))
        legacy = f"{salt.hex()}:{hashlib.pbkdf2_hmac('sha256', b'AdminPass123', salt, 150_000).hex()}"
        with self.store.db:
            self.store.db.execute('UPDATE users SET password_hash=?', (legacy,))
        with self.assertRaises(AppError) as denied:
            self.store.authenticate('admin@example.test', 'wrong-pass')
        self.assertEqual(denied.exception.status, 401)
        self.assertEqual(self.store.db.execute('SELECT password_hash FROM users').fetchone()[0], legacy)
        self.assertEqual(self.store.authenticate('admin@example.test', 'AdminPass123'), self.admin)
        upgraded = self.store.db.execute('SELECT password_hash FROM users').fetchone()[0]
        self.assertTrue(upgraded.startswith(f'pbkdf2_sha256${PASSWORD_ITERATIONS}$'))
        self.assertNotEqual(upgraded.split('$')[2], salt.hex())
        self.store.close()
        self.store = Store(str(Path(self.tmp.name) / 'test.db'))
        self.assertEqual(self.store.authenticate('admin@example.test', 'AdminPass123'), self.admin)
        self.assertEqual(self.store.db.execute('SELECT password_hash FROM users').fetchone()[0], upgraded)

    def test_versioned_password_upgrades_lower_cost_without_downgrading_stronger_hash(self):
        for iterations in [150_000, PASSWORD_ITERATIONS + 100_000]:
            stored = Store._hash('AdminPass123', iterations=iterations)
            with self.store.db:
                self.store.db.execute('UPDATE users SET password_hash=?', (stored,))
            self.store.authenticate('admin@example.test', 'AdminPass123')
            after = self.store.db.execute('SELECT password_hash FROM users').fetchone()[0]
            self.assertEqual(int(after.split('$')[1]), max(iterations, PASSWORD_ITERATIONS))
            if iterations > PASSWORD_ITERATIONS:
                self.assertEqual(after, stored)

    def test_invalid_stored_hash_is_rejected_before_expensive_work(self):
        salt, digest = '00' * 16, '11' * 32
        invalid = ['broken', 'aa:bb', f'other$600000${salt}${digest}']
        invalid.extend(f'pbkdf2_sha256${count}${salt}${digest}' for count in
                       ['0', '-1', '0600000', '9999999999999999999', str(MAX_PASSWORD_ITERATIONS + 1)])
        for stored in invalid:
            with self.subTest(stored=stored):
                with self.store.db:
                    self.store.db.execute('UPDATE users SET password_hash=?', (stored,))
                with patch('service.hashlib.pbkdf2_hmac') as derive, self.assertRaises(AppError) as failure:
                    self.store.authenticate('admin@example.test', 'AdminPass123')
                derive.assert_not_called()
                self.assertEqual(failure.exception.status, 500)
                self.assertEqual(self.store.db.execute('SELECT password_hash FROM users').fetchone()[0], stored)

    def test_unknown_account_performs_current_cost_dummy_hash(self):
        with patch.object(self.store, '_hash', wraps=Store._hash) as derive:
            with self.assertRaises(AppError) as failure:
                self.store.authenticate('missing@example.test', 'AdminPass123')
        self.assertEqual(failure.exception.status, 401)
        derive.assert_called_once_with('AdminPass123', b'\0' * 16)

    def test_password_upgrade_does_not_overwrite_concurrent_credential_change(self):
        legacy = Store._hash('AdminPass123', iterations=150_000)
        replacement = Store._hash('ReplacementPass123')
        with self.store.db:
            self.store.db.execute('UPDATE users SET password_hash=?', (legacy,))

        def change_during_upgrade(password, salt=None, iterations=PASSWORD_ITERATIONS):
            if salt is None:
                with self.store.db:
                    self.store.db.execute('UPDATE users SET password_hash=?', (replacement,))
            return Store._hash(password, salt, iterations)

        with patch.object(self.store, '_hash', side_effect=change_during_upgrade), self.assertRaises(AppError) as failure:
            self.store.authenticate('admin@example.test', 'AdminPass123')
        self.assertEqual(failure.exception.status, 401)
        self.assertEqual(self.store.db.execute('SELECT password_hash FROM users').fetchone()[0], replacement)

    def test_role_matrix_and_revocation(self):
        user = self.store.create_user('member@example.test', 'MemberPass123')
        role = self.store.create_role('成员')
        self.store.set_permissions(role['id'], ['project.view'])
        project = self.store.create_project('蜂鸟')
        self.store.add_member(project['id'], user['id'], role['id'])
        self.assertTrue(self.store.has_permission(user, project['id'], 'project.view'))
        self.assertFalse(self.store.has_permission(user, project['id'], 'members.manage'))
        self.assertEqual(self.store.list_roles()[0]['permissions'], ['project.view'])
        self.store.remove_member(project['id'], user['id'])
        self.assertFalse(self.store.has_permission(user, project['id'], 'project.view'))

    def test_non_admin_cannot_manage_roles(self):
        user = self.store.create_user('member@example.test', 'MemberPass123')
        with self.assertRaises(AppError) as result: self.store.require_admin(user)
        self.assertEqual(result.exception.status, 403)

    def test_invalid_permission_rejected(self):
        role = self.store.create_role('成员')
        with self.assertRaises(AppError): self.store.set_permissions(role['id'], ['unknown.permission'])

    def test_requirement_task_and_board_flow(self):
        manager = self.store.create_user('manager@example.test', 'ManagerPass123')
        role = self.store.create_role('项目经理')
        self.store.set_permissions(role['id'], ['project.view', 'workitems.manage'])
        project = self.store.create_project('蜂鸟')
        self.store.add_member(project['id'], manager['id'], role['id'])
        requirement = self.store.create_requirement(project['id'], '看板管理', '支持任务推进', manager['id'])
        task = self.store.create_task(project['id'], '实现任务卡片', requirement['id'],
                                      manager['id'], '2026-10-20', manager['id'], 'todo')
        self.assertEqual(task['assignee_email'], 'manager@example.test')
        moved = self.store.update_task(project['id'], task['id'], {'status': 'doing', 'version': task['version']})
        self.assertEqual(moved['status'], 'doing')
        self.assertEqual(self.store.list_requirements(project['id'])[0]['title'], '看板管理')
        self.assertEqual(self.store.list_tasks(project['id'])[0]['title'], '实现任务卡片')

    def test_task_rejects_cross_project_requirement_and_outsider(self):
        member = self.store.create_user('member@example.test', 'MemberPass123')
        outsider = self.store.create_user('outsider@example.test', 'OutsiderPass123')
        role = self.store.create_role('成员')
        first = self.store.create_project('项目一')
        second = self.store.create_project('项目二')
        self.store.add_member(first['id'], member['id'], role['id'])
        requirement = self.store.create_requirement(first['id'], '需求一', '', self.admin['id'])
        with self.assertRaises(AppError) as cross_project:
            self.store.create_task(second['id'], '错误关联', requirement['id'], None,
                                   None, self.admin['id'])
        self.assertEqual(cross_project.exception.status, 404)
        with self.assertRaises(AppError):
            self.store.create_task(first['id'], '错误负责人', None, outsider['id'], None, self.admin['id'])

    def test_removed_assignee_is_cleared_only_in_that_project(self):
        member = self.store.create_user('owner@example.test', 'MemberPass123')
        role = self.store.create_role('成员')
        first = self.store.create_project('项目一')
        second = self.store.create_project('项目二')
        for project in (first, second):
            self.store.add_member(project['id'], member['id'], role['id'])
            self.store.create_task(project['id'], '保留任务', None, member['id'], None, self.admin['id'])
        self.store.remove_member(first['id'], member['id'])
        task = self.store.list_tasks(first['id'])[0]
        self.assertIsNone(task['assignee_user_id'])
        self.assertEqual(self.store.update_task(first['id'], task['id'], {'status': 'done', 'version': task['version']})['status'], 'done')
        self.assertEqual(self.store.list_tasks(second['id'])[0]['assignee_user_id'], member['id'])

    def test_task_validates_dates_status_and_update_fields(self):
        project = self.store.create_project('蜂鸟')
        with self.assertRaises(AppError):
            self.store.create_task(project['id'], '任务', None, None, '20-10-2026', self.admin['id'])
        with self.assertRaises(AppError):
            self.store.create_task(project['id'], '任务', None, None, None, self.admin['id'], 'closed')
        task = self.store.create_task(project['id'], '任务', None, None, None, self.admin['id'])
        with self.assertRaises(AppError):
            self.store.update_task(project['id'], task['id'], {'unknown': True})

    def test_member_manager_cannot_assign_or_remove_higher_roles(self):
        manager = self.store.create_user('manager@example.test', 'ManagerPass123')
        role = self.store.create_role('成员管理')
        self.store.set_permissions(role['id'], ['members.manage'])
        high = self.store.create_role('平台管理')
        self.store.set_permissions(high['id'], ['roles.manage', 'projects.manage'])
        project = self.store.create_project('项目')
        self.store.add_member(project['id'], manager['id'], role['id'])
        with self.assertRaises(AppError) as denied:
            self.store.add_member(project['id'], manager['id'], high['id'], actor=manager)
        self.assertEqual(denied.exception.status, 403)
        self.assertFalse(self.store.has_global_permission(manager, 'roles.manage'))
        self.store.add_member(project['id'], self.admin['id'], high['id'])
        with self.assertRaises(AppError):
            self.store.add_member(project['id'], self.admin['id'], role['id'], actor=manager)
        with self.assertRaises(AppError):
            self.store.remove_member(project['id'], self.admin['id'], actor=manager)
        self.assertEqual([r['id'] for r in self.store.assignable_roles(manager, project['id'])], [role['id']])

    def test_role_editor_cannot_escalate_via_shared_role(self):
        editor = self.store.create_user('editor@example.test', 'EditorPass123')
        role = self.store.create_role('角色维护')
        self.store.set_permissions(role['id'], ['roles.manage'])
        project = self.store.create_project('项目')
        self.store.add_member(project['id'], editor['id'], role['id'])
        with self.assertRaises(AppError) as denied:
            self.store.set_permissions(role['id'], ['roles.manage', 'projects.manage'], actor=editor)
        self.assertEqual(denied.exception.status, 403)
        self.assertFalse(self.store.has_global_permission(editor, 'projects.manage'))
        self.store.set_permissions(role['id'], ['roles.manage'], actor=editor)

    def test_creator_has_project_rights_but_no_new_platform_rights(self):
        creator = self.store.create_user('creator@example.test', 'CreatorPass123')
        role = self.store.create_role('建项目')
        self.store.set_permissions(role['id'], ['projects.manage'])
        seed = self.store.create_project('授权来源')
        self.store.add_member(seed['id'], creator['id'], role['id'])
        project = self.store.create_project('新项目', creator=creator)
        self.assertIn(project, self.store.list_projects(creator))
        for permission in ('project.view', 'members.manage', 'workitems.manage'):
            self.assertTrue(self.store.has_permission(creator, project['id'], permission))
        self.assertFalse(self.store.has_global_permission(creator, 'roles.manage'))
        self.assertEqual(self.store.list_members(project['id'])[0]['is_owner'], 1)
        with self.assertRaises(AppError):
            self.store.remove_member(project['id'], creator['id'], actor=self.admin)
        task = self.store.create_task(project['id'], '创建者负责', None, creator['id'], None, creator['id'])
        self.assertEqual(task['assignee_user_id'], creator['id'])

    def test_status_change_appends_to_target_column_without_reordering_edits(self):
        project = self.store.create_project('Ordering')
        first = self.store.create_task(project['id'], 'First', None, None, None, self.admin['id'], 'doing')
        second = self.store.create_task(project['id'], 'Second', None, None, None, self.admin['id'], 'doing')
        moving = self.store.create_task(project['id'], 'Moving', None, None, None, self.admin['id'], 'todo')
        moved = self.store.update_task(project['id'], moving['id'], {'status': 'doing', 'version': moving['version']})
        self.assertEqual(moved['position'], second['position'] + 1)
        edited = self.store.update_task(project['id'], moved['id'], {'title': 'Renamed', 'version': moved['version']})
        self.assertEqual(edited['position'], moved['position'])
        ordered = self.store.query_tasks(project['id'], status='doing')['items']
        self.assertEqual([task['id'] for task in ordered], [first['id'], second['id'], moving['id']])

    def test_options_do_not_expose_unrelated_project_accounts_or_roles(self):
        manager = self.store.create_user('manager@example.test', 'MemberPass123')
        outsider = self.store.create_user('outside@example.test', 'MemberPass123')
        role = self.store.create_role('Manager')
        hidden = self.store.create_role('Hidden')
        self.store.set_permissions(role['id'], ['members.manage'])
        self.store.set_permissions(hidden['id'], ['project.view'])
        own = self.store.create_project('Own')
        other = self.store.create_project('Other')
        self.store.add_member(own['id'], manager['id'], role['id'])
        self.store.add_member(other['id'], outsider['id'], hidden['id'])
        self.assertNotIn(outsider['id'], [user['id'] for user in self.store.user_options(manager)])
        self.assertNotIn(hidden['id'], [item['id'] for item in self.store.role_options(manager)])
        self.assertTrue(self.store.has_permission(manager, own['id'], 'members.manage'))
        self.assertFalse(self.store.has_permission(manager, other['id'], 'members.manage'))
        self.assertIn(outsider['id'], [user['id'] for user in self.store.user_options(self.admin)])

    def test_management_roles_include_view_permission(self):
        role = self.store.create_role('任务管理')
        self.store.set_permissions(role['id'], ['workitems.manage'])
        self.assertEqual(set(self.store.list_roles()[0]['permissions']), {'project.view', 'workitems.manage'})

    def test_empty_permissions_revoke_project_access(self):
        """管理员清空权限后，既有成员立即失去项目访问权。"""
        member = self.store.create_user('revoke@example.test', 'MemberPass123')
        role = self.store.create_role('可撤销角色')
        self.store.set_permissions(role['id'], ['project.view'], actor=self.admin)
        project = self.store.create_project('权限回收')
        self.store.add_member(project['id'], member['id'], role['id'])
        self.assertTrue(self.store.has_permission(member, project['id'], 'project.view'))
        self.store.set_permissions(role['id'], [], actor=self.admin)
        roles = {row['id']: row for row in self.store.list_roles()}
        self.assertEqual(roles[role['id']]['permissions'], [])
        self.assertEqual(self.store.list_projects(member), [])
        with self.assertRaises(AppError) as denied:
            self.store.require_permission(member, project['id'], 'project.view')
        self.assertEqual(denied.exception.status, 403)

    def test_stale_and_missing_versions_do_not_change_task(self):
        project = self.store.create_project('项目')
        task = self.store.create_task(project['id'], '原任务', None, None, None, self.admin['id'])
        first = self.store.update_task(project['id'], task['id'], {'title': '他人修改', 'version': task['version']})
        with self.assertRaises(AppError) as stale:
            self.store.update_task(project['id'], task['id'], {'title': '旧窗口', 'version': task['version']})
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(AppError) as missing:
            self.store.update_task(project['id'], task['id'], {'status': 'done'})
        self.assertEqual(missing.exception.status, 428)
        self.assertEqual(self.store.get_task(project['id'], task['id']), first)

    def test_invalid_types_are_rejected_without_writes(self):
        project = self.store.create_project('项目')
        for title in ({'bad': 1}, ['bad'], 12, True, None):
            with self.subTest(title=title), self.assertRaises(AppError):
                self.store.create_task(project['id'], title, None, None, None, self.admin['id'])
        for value in ('20261017', '2026-02-30', 20261017):
            with self.subTest(date=value), self.assertRaises(AppError):
                self.store._due_date(value)
        task = self.store.create_task(project['id'], '任务', None, None, None, self.admin['id'])
        for field, value in [('position', -1), ('position', 1.5), ('version', True), ('requirement_id', 1.5)]:
            with self.subTest(field=field), self.assertRaises(AppError):
                self.store.update_task(project['id'], task['id'], {'version': task['version'], field: value})

    def test_legacy_database_migration_preserves_data_and_backup(self):
        path = str(Path(self.tmp.name) / 'legacy.db')
        legacy = Store(path)
        admin = legacy.setup('old@example.test', 'AdminPass123')
        project = legacy.create_project('旧项目')
        legacy.create_task(project['id'], '保留旧任务', None, None, None, admin['id'])
        legacy.db.execute('ALTER TABLE tasks DROP COLUMN version')
        legacy.db.execute('ALTER TABLE projects DROP COLUMN owner_user_id')
        legacy.db.commit()
        legacy.close()
        reopened = Store(path)
        try:
            self.assertEqual(reopened.list_tasks(project['id'])[0]['title'], '保留旧任务')
            self.assertEqual(reopened.list_tasks(project['id'])[0]['version'], 1)
            self.assertTrue(Path(path + '.pre-hardening.bak').is_file())
            with closing(sqlite3.connect(path + '.pre-hardening.bak')) as snapshot:
                self.assertEqual(snapshot.execute('SELECT title FROM tasks').fetchone()[0], '保留旧任务')
        finally:
            reopened.close()


if __name__ == '__main__': unittest.main()
