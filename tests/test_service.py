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


if __name__ == '__main__': unittest.main()
