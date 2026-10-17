# Feature 分支说明：API 与界面完善

**分支名称：** `feature/api-improvements`  
**创建日期：** 2026-10-17  
**基于分支：** `main` (commit `207e7b1`)

---

## 分支目的

本分支实现了 Sprint 2 的 API 接口扩展和前端交互增强，包括需求管理、任务删除、角色管理等功能的完整 CRUD 支持。

---

## 改进内容

### 后端 API 新增

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/projects/{id}/requirements/{id}` | GET | 获取单条需求 |
| `/api/projects/{id}/requirements/{id}` | PUT | 编辑需求 |
| `/api/projects/{id}/requirements/{id}` | DELETE | 删除需求 |
| `/api/projects/{id}/tasks/{id}` | DELETE | 删除任务 |
| `/api/roles/{id}` | PUT | 重命名角色 |
| `/api/roles/{id}` | DELETE | 删除角色 |

### 前端改进

- **错误处理**：401 会话过期自动退出到登录页
- **需求管理**：列表增加编辑/删除按钮，支持弹窗编辑
- **任务管理**：编辑弹窗增加删除按钮
- **角色管理**：权限矩阵增加重命名/删除操作
- **表单增强**：`submit()` 函数支持 PUT 方法

### 业务层新增方法

- `get_requirement()` / `update_requirement()` / `delete_requirement()`
- `delete_task()`
- `update_role_name()` / `delete_role()`

---

## 测试验证

- **单元测试**：11 项全部通过
- **JS 语法检查**：`node --check app.js` 通过
- **覆盖场景**：
  - Sprint 1 权限回归
  - Sprint 2 需求任务链路
  - 跨项目隔离
  - 移除成员后负责人释放
  - 越权阻断

---

## 文件变更清单

```
modified:   app.js
modified:   index.html
modified:   server.py
modified:   service.py
modified:   styles.css
new file:   docs/plans/2026-10-17-api-improvements.md
```

---

## 合并建议

1. 审查代码变更
2. 本地运行测试：`python -m unittest discover -s tests -v`
3. 浏览器验证主要功能
4. 合并到 `main` 分支

---

## 相关链接

- 详细改进文档：[2026-10-17-api-improvements.md](./2026-10-17-api-improvements.md)
- Pull Request：https://github.com/Jorungandr/aimanager-sprint1/pull/new/feature/api-improvements
