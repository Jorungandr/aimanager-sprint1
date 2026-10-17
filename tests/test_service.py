"""用户、角色、项目及权限回收的关键路径。"""
import sys
from pathlib import Path
import tempfile
import unittest
import sqlite3
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import AppError, Store


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

    def test_management_roles_include_view_permission(self):
        role = self.store.create_role('任务管理')
        self.store.set_permissions(role['id'], ['workitems.manage'])
        self.assertEqual(set(self.store.list_roles()[0]['permissions']), {'project.view', 'workitems.manage'})

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
