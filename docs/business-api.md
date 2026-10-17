# 后端业务对接说明

维护人：刘皓天。HTTP接口和页面由唐晨飞对接；专项测试可由王昊洋继续维护。本文件约定业务函数，不表示相应HTTP路由和页面入口已经实现。

## 稳定入口

继续使用`from service import Store, AppError`，以及`Store(path)`、`store.db`、`store.close()`。数据库为SQLite；不需要安装MySQL或改变现有启动方式。

`database.py`负责SQLite连接、建表、备份、增量迁移和索引初始化；`service.py`保留业务规则。`SCHEMA_VERSION`仍可从`service`导入。`database.open_database(path)`返回调用者负责关闭的连接，初始化失败自动关闭；不支持的新版数据库由`Store`保持映射为`AppError(409)`。HTTP和页面无需因拆分而修改导入或调用。

## 需求函数

| 函数 | 参数及行为 |
| --- | --- |
| `list_requirements(project_id, include_archived=True)` | 默认包含归档历史；新关联需求的选择器使用`include_archived=False` |
| `get_requirement(project_id, requirement_id)` | 读取同项目内需求，跨项目或不存在返回404 |
| `create_requirement(project_id, title, description, created_by)` | 标题1—80字，说明最多500字，可为空字符串 |
| `update_requirement(project_id, requirement_id, changes)` | 允许`title`、`description`和`version`；至少修改一项文本字段 |
| `archive_requirement(project_id, requirement_id, version)` | 归档但不删除需求和已有任务 |
| `restore_requirement(project_id, requirement_id, version)` | 恢复后可重新关联任务 |

需求返回字段为`id`、`project_id`、`title`、`description`、`created_by`、`created_at`、`version`、`archived`。`archived`为布尔值；版本从1开始，实际修改成功后递增。创建人和创建时间不可编辑。

```python
current = store.get_requirement(project_id, requirement_id)
updated = store.update_requirement(project_id, requirement_id, {
    'title': '更新后的需求标题',
    'version': current['version'],
})
archived = store.archive_requirement(project_id, requirement_id, updated['version'])
restored = store.restore_requirement(project_id, requirement_id, archived['version'])
```

## 权限和错误

HTTP入口必须先认证，再用`store.require_permission(user, project_id, 'project.view')`检查读取权限；写入用`workitems.manage`。不要从请求体信任`created_by`，应使用认证用户编号。上述需求业务函数负责数据规则，不代替HTTP鉴权；沿用服务器已有的数据库锁。

| 状态码 | 含义及页面处理 |
| --- | --- |
| 400 | 字段、类型或长度不合法，展示具体提示 |
| 403 | 无项目权限，由HTTP入口检查 |
| 404 | 需求不存在或不属于当前项目 |
| 409 | 旧版本冲突、编辑归档需求或新关联归档需求；保留输入，提示刷新或先恢复 |
| 428 | 缺少读取时的版本号，重新读取后提交 |

编辑归档需求前必须恢复。归档/恢复重复请求若携带当前版本且状态已匹配，返回原记录，不递增版本；旧版本仍返回409。不能直接通过`update_requirement`改`archived`或创建人。

## 归档对任务的影响

- 禁止新建任务关联归档需求，也禁止其他任务新关联该需求。
- 原有关联任务可保留关联并修改状态、负责人、日期和标题。
- 可以解除旧关联；解除后不能重新关联仍归档的需求。
- 不自动删除或完成任何任务，不将需求归档视为故事已验收。
- 任务更新仍须携带任务自己的`version`，不能使用需求版本替代。

## 数据库与验证

正常数据路径为`data/aimanager.db`；迁移前的备份可能为`.pre-hardening.bak`和`.pre-requirements.bak`。备份含私人数据，不提交到Git。旧数据不清空；`PRAGMA user_version`记录结构版本，新于程序支持范围的数据库拒绝打开。

运行`python -m unittest discover -s tests -v`，自动包含`tests/test_requirements_service.py`的17项专项检查；全库共44项后端测试。所有测试使用临时数据库，覆盖归档恢复、跨项目、版本冲突、并发新关联、旧库迁移、索引查询计划及初始化失败的连接释放。
