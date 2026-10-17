"""用户、角色、项目及权限回收的关键路径。"""
import sys
from pathlib import Path
import tempfile
import unittest

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
        moved = self.store.update_task(project['id'], task['id'], {'status': 'doing'})
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
        self.assertEqual(self.store.update_task(first['id'], task['id'], {'status': 'done'})['status'], 'done')
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


if __name__ == '__main__': unittest.main()
