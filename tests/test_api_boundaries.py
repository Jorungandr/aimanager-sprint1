"""HTTP 层边界测试：需求/任务路由、权限、版本控制、错误处理"""
import json
import sys
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_server


class ApiBoundaryTests(unittest.TestCase):
    """HTTP API 边界测试"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.tmp.name) / 'test.db'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = build_opener(HTTPCookieProcessor(CookieJar()))
        self.manager = build_opener(HTTPCookieProcessor(CookieJar()))
        self.viewer = build_opener(HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.store.close()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, opener, path, method='GET', data=None):
        """发送请求并返回 (status, data)"""
        # URL 编码路径和查询参数（处理中文等非 ASCII 字符）
        parsed = urlparse(self.base + path)
        encoded_path = quote(parsed.path, safe='/:')
        encoded_query = quote(parsed.query, safe='=&')
        encoded_url = urlunparse((parsed.scheme, parsed.netloc, encoded_path,
                                   parsed.params, encoded_query, parsed.fragment))
        body = json.dumps(data).encode('utf-8') if data is not None else None
        request = Request(encoded_url, data=body, method=method,
                         headers={'Content-Type': 'application/json'})
        if method in ('POST', 'PUT', 'DELETE'):
            with opener.open(self.base + '/api/csrf', timeout=5) as response:
                request.add_header('X-CSRF-Token', json.load(response)['csrf_token'])
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode('utf-8'))

    def setup_users_and_project(self):
        """初始化管理员、项目经理、普通成员和项目"""
        # 初始化管理员
        status, admin = self.req(self.admin, '/api/setup', 'POST',
                                {'email': 'admin@test.com', 'password': 'AdminPass123'})
        self.assertEqual(status, 201)

        # 创建项目经理
        status, manager = self.req(self.admin, '/api/users', 'POST',
                                   {'email': 'manager@test.com', 'password': 'ManagerPass123'})
        self.assertEqual(status, 201)

        # 创建普通成员
        status, viewer = self.req(self.admin, '/api/users', 'POST',
                                  {'email': 'viewer@test.com', 'password': 'ViewerPass123'})
        self.assertEqual(status, 201)

        # 创建角色
        status, manager_role = self.req(self.admin, '/api/roles', 'POST', {'name': '项目经理'})
        self.assertEqual(status, 201)
        status, _ = self.req(self.admin, f'/api/roles/{manager_role["id"]}/permissions', 'PUT',
                            {'permissions': ['project.view', 'workitems.manage', 'members.manage']})
        self.assertEqual(status, 200)

        status, viewer_role = self.req(self.admin, '/api/roles', 'POST', {'name': '查看者'})
        self.assertEqual(status, 201)
        status, _ = self.req(self.admin, f'/api/roles/{viewer_role["id"]}/permissions', 'PUT',
                            {'permissions': ['project.view']})
        self.assertEqual(status, 200)

        # 创建项目
        status, project = self.req(self.admin, '/api/projects', 'POST', {'name': '测试项目'})
        self.assertEqual(status, 201)

        # 添加成员
        status, _ = self.req(self.admin, f'/api/projects/{project["id"]}/members', 'POST',
                            {'user_id': manager['id'], 'role_id': manager_role['id']})
        self.assertEqual(status, 201)
        status, _ = self.req(self.admin, f'/api/projects/{project["id"]}/members', 'POST',
                            {'user_id': viewer['id'], 'role_id': viewer_role['id']})
        self.assertEqual(status, 201)

        # 登录
        status, _ = self.req(self.manager, '/api/login', 'POST',
                            {'email': 'manager@test.com', 'password': 'ManagerPass123'})
        self.assertEqual(status, 200)
        status, _ = self.req(self.viewer, '/api/login', 'POST',
                            {'email': 'viewer@test.com', 'password': 'ViewerPass123'})
        self.assertEqual(status, 200)

        return project, manager, viewer

    def test_requirement_get_requires_project_view(self):
        """需求单条读取需要 project.view 权限"""
        project, manager, viewer = self.setup_users_and_project()
        pid = project['id']

        # 创建需求
        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']

        # 项目经理可以读取
        status, data = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(data['title'], '测试需求')
        self.assertIn('version', data)
        self.assertIn('archived', data)

        # 查看者可以读取（有 project.view）
        status, data = self.req(self.viewer, f'/api/projects/{pid}/requirements/{rid}', 'GET')
        self.assertEqual(status, 200)

    def test_requirement_update_requires_workitems_manage(self):
        """需求编辑需要 workitems.manage 权限"""
        project, manager, viewer = self.setup_users_and_project()
        pid = project['id']

        # 创建需求
        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']
        version = req['version']

        # 查看者无法编辑（无 workitems.manage）
        status, error = self.req(self.viewer, f'/api/projects/{pid}/requirements/{rid}', 'PUT',
                                {'title': '新标题', 'version': version})
        self.assertEqual(status, 403)

        # 项目经理可以编辑
        status, updated = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'PUT',
                                   {'title': '新标题', 'version': version})
        self.assertEqual(status, 200)
        self.assertEqual(updated['title'], '新标题')
        self.assertEqual(updated['version'], version + 1)

    def test_requirement_update_requires_version(self):
        """需求编辑缺少版本号返回 428"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']

        # 缺少版本号
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'PUT',
                                {'title': '新标题'})
        self.assertEqual(status, 428)
        self.assertIn('版本号', error['error'])

    def test_requirement_update_version_conflict(self):
        """需求编辑版本冲突返回 409"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']
        old_version = req['version']

        # 第一次编辑成功
        status, _ = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'PUT',
                            {'title': '第一次修改', 'version': old_version})
        self.assertEqual(status, 200)

        # 使用旧版本号再次编辑
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'PUT',
                                {'title': '第二次修改', 'version': old_version})
        self.assertEqual(status, 409)
        self.assertIn('刷新', error['error'])

    def test_requirement_archive_requires_workitems_manage(self):
        """需求归档需要 workitems.manage 权限"""
        project, manager, viewer = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']
        version = req['version']

        # 查看者无法归档
        status, error = self.req(self.viewer, f'/api/projects/{pid}/requirements/{rid}/archive', 'POST',
                                {'version': version})
        self.assertEqual(status, 403)

        # 项目经理可以归档
        status, archived = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}/archive', 'POST',
                                    {'version': version})
        self.assertEqual(status, 200)
        self.assertTrue(archived['archived'])

    def test_requirement_restore_requires_workitems_manage(self):
        """需求恢复需要 workitems.manage 权限"""
        project, manager, viewer = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']
        version = req['version']

        # 归档
        status, archived = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}/archive', 'POST',
                                    {'version': version})
        self.assertEqual(status, 200)
        archived_version = archived['version']

        # 查看者无法恢复
        status, error = self.req(self.viewer, f'/api/projects/{pid}/requirements/{rid}/restore', 'POST',
                                {'version': archived_version})
        self.assertEqual(status, 403)

        # 项目经理可以恢复
        status, restored = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}/restore', 'POST',
                                    {'version': archived_version})
        self.assertEqual(status, 200)
        self.assertFalse(restored['archived'])

    def test_requirement_delete_rejected(self):
        """需求永久删除返回 405"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']

        # 尝试删除
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'DELETE')
        self.assertEqual(status, 405)
        self.assertIn('归档', error['error'])

    def test_requirement_unsupported_method(self):
        """需求路由不支持的方法返回 405"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, req = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                              {'title': '测试需求', 'description': '描述'})
        self.assertEqual(status, 201)
        rid = req['id']

        # 列表路由不支持 PUT/DELETE
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements', 'PUT',
                                {'title': '新标题'})
        self.assertEqual(status, 405)

        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements', 'DELETE')
        self.assertEqual(status, 405)

        # 单条路由不支持 POST
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements/{rid}', 'POST',
                                {'title': '新标题'})
        self.assertEqual(status, 405)

    def test_requirement_not_found(self):
        """需求不存在返回 404"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements/99999', 'GET')
        self.assertEqual(status, 404)
        self.assertIn('不存在', error['error'])

    def test_task_query_requires_project_view(self):
        """任务分页查询需要 project.view 权限"""
        project, manager, viewer = self.setup_users_and_project()
        pid = project['id']

        # 创建任务
        status, task = self.req(self.manager, f'/api/projects/{pid}/tasks', 'POST',
                               {'title': '测试任务', 'status': 'todo'})
        self.assertEqual(status, 201)

        # 项目经理可以查询
        status, data = self.req(self.manager, f'/api/projects/{pid}/tasks/query', 'GET')
        self.assertEqual(status, 200)
        self.assertIn('items', data)
        self.assertIn('total', data)
        self.assertIn('page', data)
        self.assertEqual(data['total'], 1)

        # 查看者可以查询（有 project.view）
        status, data = self.req(self.viewer, f'/api/projects/{pid}/tasks/query', 'GET')
        self.assertEqual(status, 200)

    def test_task_query_with_filters(self):
        """任务分页查询支持筛选参数"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        # 创建多个任务
        for i in range(3):
            status, _ = self.req(self.manager, f'/api/projects/{pid}/tasks', 'POST',
                                {'title': f'任务{i}', 'status': 'todo'})
            self.assertEqual(status, 201)

        # 按状态筛选
        status, data = self.req(self.manager, f'/api/projects/{pid}/tasks/query?status=todo', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(data['total'], 3)

        # 分页
        status, data = self.req(self.manager, f'/api/projects/{pid}/tasks/query?page=1&page_size=2', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(len(data['items']), 2)
        self.assertEqual(data['total_pages'], 2)

        # 关键词搜索
        status, data = self.req(self.manager, f'/api/projects/{pid}/tasks/query?keyword=任务1', 'GET')
        self.assertEqual(status, 200)
        self.assertEqual(data['total'], 1)

    def test_task_query_unsupported_method(self):
        """任务查询路由不支持的方法返回 405"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, error = self.req(self.manager, f'/api/projects/{pid}/tasks/query', 'POST',
                                {'title': '测试'})
        self.assertEqual(status, 405)

        status, error = self.req(self.manager, f'/api/projects/{pid}/tasks/query', 'PUT',
                                {'title': '测试'})
        self.assertEqual(status, 405)

    def test_task_list_returns_array(self):
        """任务列表接口返回数组（保持兼容）"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        status, task = self.req(self.manager, f'/api/projects/{pid}/tasks', 'POST',
                               {'title': '测试任务', 'status': 'todo'})
        self.assertEqual(status, 201)

        # 列表接口返回数组
        status, data = self.req(self.manager, f'/api/projects/{pid}/tasks', 'GET')
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_unauthenticated_access(self):
        """未登录访问受保护接口返回 401"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        # 未登录的客户端
        unauth = build_opener(HTTPCookieProcessor(CookieJar()))

        status, error = self.req(unauth, f'/api/projects/{pid}/requirements', 'GET')
        self.assertEqual(status, 401)

        status, error = self.req(unauth, f'/api/projects/{pid}/tasks', 'GET')
        self.assertEqual(status, 401)

        status, error = self.req(unauth, '/api/me', 'GET')
        self.assertEqual(status, 401)

    def test_invalid_json_returns_400(self):
        """无效 JSON 返回 400"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        # 发送无效 JSON
        request = Request(self.base + f'/api/projects/{pid}/requirements',
                         data=b'invalid json', method='POST',
                         headers={'Content-Type': 'application/json'})
        with self.manager.open(self.base + '/api/csrf', timeout=5) as response:
            request.add_header('X-CSRF-Token', json.load(response)['csrf_token'])
        try:
            with self.manager.open(request) as response:
                status = response.status
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            with error:
                status = error.code
                data = json.loads(error.read().decode('utf-8'))

        self.assertEqual(status, 400)
        self.assertIn('JSON', data['error'])

    def test_missing_required_fields(self):
        """缺少必填字段返回 400"""
        project, manager, _ = self.setup_users_and_project()
        pid = project['id']

        # 创建需求缺少标题
        status, error = self.req(self.manager, f'/api/projects/{pid}/requirements', 'POST',
                                {'description': '描述'})
        self.assertEqual(status, 400)

        # 创建任务缺少标题
        status, error = self.req(self.manager, f'/api/projects/{pid}/tasks', 'POST',
                                {'status': 'todo'})
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
