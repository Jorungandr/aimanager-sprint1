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

## 任务组合筛选与分页

`list_tasks(project_id)`仍返回完整列表，兼容现有HTTP和页面。新增业务函数（HTTP路由待唐晨飞对接）：

```python
result = store.query_tasks(project_id, status='todo', unassigned=True, page=1, page_size=20)
```

可选关键字参数：`assignee_user_id`指定负责人；`unassigned=True`仅未指派（不能同时指定负责人）；`status`为现有四种任务状态；`requirement_id`为本项目需求编号；`keyword`搜索任务标题或关联需求标题，最长100字，百分号和下划线按普通文本匹配。未传筛选条件表示全部，组合条件之间为“且”。不存在的负责人返回空结果，跨项目或不存在的需求返回404；历史任务可按归档需求筛选。

`page`从1开始、最大1000000，`page_size`从1到100。返回`items`、`total`、`page`、`page_size`、`total_pages`；空结果总页数为0，超出末页返回空列表但保留实际总数。不承诺跨两次分页请求的快照一致性；每次调用内总数与列表使用同一读事务。排序沿用看板状态顺序，再按position、id保证相同排序值下的确定性。

HTTP入口仍须检查`project.view`并使用现有数据库锁。任务筛选支持基线US-28和US-24的使用场景；分页属于工程改进，不额外计为已验收用户故事。是否完成故事、速率和燃尽情况须以真实团队过程与DoD为准。

## 数据库与验证

正常数据路径为`data/aimanager.db`；迁移前的备份可能为`.pre-hardening.bak`和`.pre-requirements.bak`。备份含私人数据，不提交到Git。旧数据不清空；`PRAGMA user_version`记录结构版本，新于程序支持范围的数据库拒绝打开。

运行`python -m unittest discover -s tests -v`，自动包含需求数据库专项17项和任务查询专项9项；全库共53项后端测试。所有测试使用隔离数据库，覆盖归档恢复、跨项目、版本冲突、并发新关联、旧库迁移、查询索引、组合筛选、分页边界和事务兼容性。
