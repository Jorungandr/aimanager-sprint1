# Sprint 2 API 与界面完善改进说明

**日期：** 2026-10-17
**范围：** server.py、app.js、service.py、index.html、styles.css

---

## 一、后端 API 新增接口

### 1. 需求管理（Requirements）

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/projects/{id}/requirements/{id}` | `workitems.manage` | 获取单条需求详情 |
| PUT | `/api/projects/{id}/requirements/{id}` | `workitems.manage` | 编辑需求标题和说明 |
| DELETE | `/api/projects/{id}/requirements/{id}` | `workitems.manage` | 删除需求，关联任务的 `requirement_id` 自动置空 |

### 2. 任务管理（Tasks）

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| DELETE | `/api/projects/{id}/tasks/{id}` | `workitems.manage` | 删除任务，不可恢复 |

### 3. 角色管理（Roles）

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| PUT | `/api/roles/{id}` | `roles.manage` | 修改角色名称（1-30 字，不可重复） |
| DELETE | `/api/roles/{id}` | `roles.manage` | 删除角色，关联的成员关系级联清理 |

---

## 二、业务层新增方法（service.py）

| 方法 | 说明 |
|------|------|
| `get_requirement(project_id, requirement_id)` | 获取单条需求，不存在或不属于当前项目时抛 404 |
| `update_requirement(project_id, requirement_id, changes)` | 更新需求标题和说明，校验长度限制 |
| `delete_requirement(project_id, requirement_id)` | 删除需求，外键 `ON DELETE SET NULL` 自动解除任务关联 |
| `delete_task(project_id, task_id)` | 删除任务 |
| `update_role_name(role_id, name)` | 修改角色名称，校验唯一性 |
| `delete_role(role_id)` | 删除角色，`ON DELETE CASCADE` 级联删除权限配置 |

---

## 三、前端改进（app.js / index.html）

### 1. 错误处理增强

- **401 会话过期自动退出：** `api()` 函数检测到 401 响应时，自动隐藏工作区、显示登录面板，提示"会话已过期，请重新登录"
- **submit 函数支持 PUT 方法：** 新增第四个参数 `method`，默认 `'POST'`，编辑表单可传入 `'PUT'`

### 2. 需求管理 UI

- 需求列表每项增加"编辑"和"删除"按钮（仅 `workitems.manage` 权限可见）
- 新增需求编辑弹窗（`#requirementDialog`），可修改标题和说明
- 删除需求前弹出确认提示

### 3. 任务管理 UI

- 任务编辑弹窗（`#taskDialog`）新增"删除任务"按钮
- 删除前弹出确认提示，操作不可恢复

### 4. 角色管理 UI

- 权限矩阵每行增加"重命名"和"删除"按钮（仅 `roles.manage` 权限可见）
- 重命名使用 `prompt` 输入新名称
- 删除前弹出确认提示

---

## 四、样式补充（styles.css）

| 选择器 | 说明 |
|--------|------|
| `.requirement-actions` | 需求操作按钮容器，行内 flex 布局 |
| `.requirement-actions button` | 需求操作按钮尺寸（28px 高、11px 字号） |
| `.dialog-actions` | 弹窗底部操作区，flex 布局带间距 |
| `.dialog-actions button` | 弹窗操作按钮不拉伸 |

---

## 五、数据完整性保障

| 场景 | 处理方式 |
|------|----------|
| 删除需求 | 任务 `requirement_id` 通过 `ON DELETE SET NULL` 自动置空，任务保留 |
| 删除角色 | `role_permissions` 通过 `ON DELETE CASCADE` 级联删除 |
| 删除角色 | `memberships` 表未级联，需先手动移除成员或重新分配角色 |
| 移除成员 | 该项目任务 `assignee_user_id` 自动置空，其他项目不受影响 |

---

## 六、验证结果

- **单元测试：** 11 项全部通过（`python -m unittest discover -s tests -v`）
- **JS 语法检查：** `node --check app.js` 通过
- **覆盖场景：** Sprint 1 权限回归、Sprint 2 需求任务链路、跨项目隔离、移除成员后负责人释放、越权阻断

---

## 七、接口总览

本次改进后完整 API 列表：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 检查是否需要初始化管理员 |
| POST | `/api/setup` | 初始化管理员 |
| POST | `/api/login` | 登录 |
| POST | `/api/logout` | 退出 |
| GET | `/api/me` | 当前用户信息及能力 |
| GET/POST | `/api/users` | 用户列表/创建（管理员） |
| GET | `/api/user-options` | 用户下拉选项（成员管理权限） |
| GET/POST | `/api/roles` | 角色列表/创建 |
| GET | `/api/role-options` | 角色下拉选项 |
| PUT | `/api/roles/{id}/permissions` | 设置角色权限 |
| **PUT** | **`/api/roles/{id}`** | **修改角色名称（新增）** |
| **DELETE** | **`/api/roles/{id}`** | **删除角色（新增）** |
| GET/POST | `/api/projects` | 项目列表/创建 |
| GET | `/api/projects/{id}/manage-check` | 检查成员管理权限 |
| GET | `/api/projects/{id}/workitems/manage-check` | 检查需求任务管理权限 |
| GET/POST | `/api/projects/{id}/requirements` | 需求列表/创建 |
| **GET** | **`/api/projects/{id}/requirements/{id}`** | **获取单条需求（新增）** |
| **PUT** | **`/api/projects/{id}/requirements/{id}`** | **编辑需求（新增）** |
| **DELETE** | **`/api/projects/{id}/requirements/{id}`** | **删除需求（新增）** |
| GET/POST | `/api/projects/{id}/tasks` | 任务列表/创建 |
| PUT | `/api/projects/{id}/tasks/{id}` | 编辑任务 |
| **DELETE** | **`/api/projects/{id}/tasks/{id}`** | **删除任务（新增）** |
| GET/POST | `/api/projects/{id}/members` | 成员列表/添加 |
| DELETE | `/api/projects/{id}/members/{id}` | 移除成员 |
