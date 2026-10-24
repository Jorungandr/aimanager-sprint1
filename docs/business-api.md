# 后端业务对接说明

维护人：刘皓天。HTTP接口和页面交互由唐晨飞对接，界面体验由殷子淇完善，专项测试可由王昊洋继续维护。需求生命周期和任务分页HTTP接口已经整合到个人集成分支；页面支持需求编辑、归档恢复，任务看板仍使用完整数组在浏览器内筛选，不使用分页结果代替项目总进度。

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

POST/PUT必须携带`Content-Type: application/json`，否则返回415。响应错误格式仍保持`{"error":"提示文本"}`以兼容已交付客户端，不在本轮改成嵌套对象。过载返回503和`Retry-After`，登录限速返回429和实际退避秒数。平台管理员读取不存在项目返回404；普通账号无项目权限仍返回403。

所有POST/PUT/DELETE（包括`/api/setup`、`/api/login`、`/api/logout`）必须携带有效CSRF Cookie及对应`X-CSRF-Token`头，否则在读取请求体、校验登录和执行数据库操作前返回403；Origin缺失也不例外。客户端先以同一Cookie会话调用`GET /api/csrf`，读取JSON中的`csrf_token`并保留响应Cookie。令牌为HMAC签名双提交值，绑定当前sid，登录/退出轮换sid或服务器重启后须重新获取；尚未登录也可获取，以保护登录及初始化。令牌端点返回`Cache-Control: no-store`，Cookie使用`HttpOnly; SameSite=Strict; Path=/`，不增加表结构，也不代替用户权限判定。有效Cookie的重复获取会复用令牌，避免无故影响同会话其他标签页；前端只合并正在进行的令牌获取，不长期缓存，不自动重放失败写请求。

`/api/user-options`不再向普通项目管理者返回全平台账号，只返回其可管理项目已有成员及创建者。新账号首次加入项目由平台管理员操作。`/api/role-options`对无任何可访问项目的普通用户返回403，其余非角色管理员只能看到可访问项目正在使用的角色；项目成员授权仍使用`/api/projects/{id}/role-options`提供的授权上限裁剪结果。

已接入的HTTP接口如下，响应直接返回业务对象，不增加`data`包装层：

| 方法与路径 | 请求与返回 |
| --- | --- |
| `GET /api/projects/{pid}/requirements` | 返回包含归档历史的需求数组 |
| `GET /api/projects/{pid}/requirements/{rid}` | 返回单条需求 |
| `POST /api/projects/{pid}/requirements` | `title`、`description`；201返回新需求，创建人来自登录会话 |
| `PUT /api/projects/{pid}/requirements/{rid}` | `title`和/或`description`及`version`；200返回更新后的需求 |
| `POST /api/projects/{pid}/requirements/{rid}/archive` | `{"version": 1}`；200返回归档需求 |
| `POST /api/projects/{pid}/requirements/{rid}/restore` | `{"version": 2}`；200返回恢复后的需求 |
| `GET /api/projects/{pid}/tasks` | 保持返回完整任务数组 |
| `GET /api/projects/{pid}/tasks/query` | 返回分页对象，查询参数见下文 |

需求和任务永久删除请求返回405，不修改数据；归档与恢复只能使用POST。错误响应为`{"error": "具体说明"}`，不返回内部异常详情。

HTTP入口必须先认证，再用`store.require_permission(user, project_id, 'project.view')`检查读取权限；写入用`workitems.manage`。不要从请求体信任`created_by`，应使用认证用户编号。上述需求业务函数负责数据规则，不代替HTTP鉴权；沿用服务器已有的数据库锁。

| 状态码 | 含义及页面处理 |
| --- | --- |
| 400 | 字段、类型或长度不合法，展示具体提示 |
| 401 | 未登录或会话失效，关闭编辑弹窗并回到登录页 |
| 403 | 无项目权限，由HTTP入口检查 |
| 404 | 需求不存在或不属于当前项目 |
| 405 | 不支持的方法或永久删除操作 |
| 409 | 旧版本冲突、编辑归档需求或新关联归档需求；保留输入，提示刷新或先恢复 |
| 428 | 缺少读取时的版本号，重新读取后提交 |

编辑归档需求前必须恢复。归档/恢复重复请求若携带当前版本且状态已匹配，返回原记录，不递增版本；旧版本仍返回409。不能直接通过`update_requirement`改`archived`或创建人。

## 归档对任务的影响

- 禁止新建任务关联归档需求，也禁止其他任务新关联该需求。
- 原有关联任务可保留关联并修改状态、负责人、日期和标题。
- 可以解除旧关联；解除后不能重新关联仍归档的需求。
- 不自动删除或完成任何任务，不将需求归档视为故事已验收。
- 任务更新仍须携带任务自己的`version`，不能使用需求版本替代。
- 任务更新至少携带一个业务字段，仅携带`version`返回400且版本不变。目标列排序值已达SQLite整数上限时，追加任务或默认移入返回400，不产生部分写入。

## 任务组合筛选与分页

`list_tasks(project_id)`仍返回完整列表，兼容现有HTTP和页面。`GET /api/projects/{pid}/tasks/query`已接入以下业务函数：

```python
result = store.query_tasks(project_id, status='todo', unassigned=True, page=1, page_size=20)
```

可选关键字参数：`assignee_user_id`指定负责人；`unassigned=True`仅未指派（不能同时指定负责人）；`status`为现有四种任务状态；`requirement_id`为本项目需求编号；`keyword`搜索任务标题或关联需求标题，最长100字，百分号和下划线按普通文本匹配。未传筛选条件表示全部，组合条件之间为“且”。不存在的负责人返回空结果，跨项目或不存在的需求返回404；历史任务可按归档需求筛选。

`page`从1开始、最大1000000，`page_size`从1到100。返回`items`、`total`、`page`、`page_size`、`total_pages`；空结果总页数为0，超出末页返回空列表但保留实际总数。不承诺跨两次分页请求的快照一致性；每次调用内总数与列表使用同一读事务。排序沿用看板状态顺序，再按position、id保证相同排序值下的确定性。

HTTP入口仍须检查`project.view`并使用现有数据库锁。任务筛选支持基线US-28和US-24的使用场景；分页属于工程改进，不额外计为已验收用户故事。是否完成故事、速率和燃尽情况须以真实团队过程与DoD为准。

HTTP查询使用相同的snake_case名称：`page`、`page_size`、`assignee_user_id`、`unassigned`、`status`、`requirement_id`、`keyword`。布尔值严格为`true`或`false`；未知或重复参数返回400，空数字、空状态及空布尔值不合法，`keyword=`允许空搜索。不接受旧分支的`pageSize`、`assignee`、`search`；调用方不可自行更换名称。

## 数据库与验证

正常数据路径为`data/aimanager.db`；迁移前的备份可能为`.pre-hardening.bak`和`.pre-requirements.bak`。备份含私人数据，不提交到Git。旧数据不清空；`PRAGMA user_version`记录结构版本，新于程序支持范围的数据库拒绝打开。

升级前先生成原库快照，建表、列迁移、权限修正、索引及版本号统一提交；任一步骤失败均回滚本次初始化。快照先写入唯一临时文件，完成后再发布为备份文件；失败时清理临时文件并终止升级。已有同名备份不覆盖，备份路径被目录占用则报错。已有备份可能来自更早的升级，不等于最近一次业务数据快照；自动备份不能替代日常备份。

### 恢复演练与实际恢复

1. 停止服务及所有访问数据库的进程，保留当前数据库和可能存在的`-wal`、`-shm`文件，不直接覆盖或删除它们。升级也应在停服、无其他写入者的条件下进行。
2. 将选定备份通过SQLite的`Connection.backup()`恢复到一个新文件；不要直接对唯一备份启动程序，因为启动可能升级其结构。
3. 在副本上检查`PRAGMA integrity_check`结果为`ok`、`PRAGMA foreign_key_check`无结果，并检查项目、需求、任务等关键业务数据。恢复到旧备份会失去备份之后的变更，须由负责人确认。
4. 用匹配版本的程序打开副本；当前程序会按需要重新升级。确认测试及人工业务检查通过后，才在停服状态下切换数据库路径并启动服务；失败则保留原文件排查。不提供自动覆盖正式库的恢复入口。

运行`python -m unittest discover -s tests -v`，默认包含HTTP契约测试；2026-10-18复核共95项后端测试通过，后续以实际输出为准。契约可单独运行`python tests/contracts/business_api_contract.py -v`。所有测试使用隔离数据库，覆盖归档恢复、跨项目、版本冲突、并发新关联、旧库迁移、查询索引、组合筛选、分页边界、重启持久化和失败不写入。故障测试覆盖迁移失败全量回滚、备份异常和恢复后重新升级；不代表已对正式数据完成恢复验收。

`python scripts/check_database.py "数据库副本路径"`执行只读健康检查，退出码0为正常、1为发现异常、2为无法检查。不打开默认正式库、不调用Store、不迁移或修复，不创建不存在的库。检查包括结构版本、必需列、完整性、外键、跨项目需求关联和负责人资格（项目创建者属于合法负责人）；输出不含业务文本，每类定位编号最多100条。交付检查宜停服后在数据库副本上进行。

## Sprint 3 统计与变更事件

Sprint 3 在任务、需求之外新增只读统计接口与统一变更事件表，供仪表盘、下钻与角色变更追溯使用。四个入口均要求 `project.view`，全部为 `GET`；写操作仍走原有任务与成员接口。

```text
GET /api/projects/{pid}/stats?days=14
GET /api/projects/{pid}/stats/tasks?dimension=status&value=done
GET /api/projects/{pid}/risks?limit=10
GET /api/projects/{pid}/audit?entity_type=task&entity_id=12&limit=20
```

`stats` 返回 `as_of`、`window_days`、`total`、`completed`、`overdue`、`due_soon`、`completion_rate`、`by_status`、`by_assignee`、`by_requirement`、`trend`。口径如下：`total` 为当前项目全部任务；`completed` 为状态等于 `done` 的任务；`overdue` 为未完成且 `due_date` 早于基准日；`due_soon` 为未完成且截止日在基准日起三天内；`completion_rate` 为完成数除以总数并保留一位小数，总数为 0 时为 0。`by_assignee` 含“未指派”分组；`by_requirement` 统计已挂接任务的需求并追加“未关联需求”分组，归档需求的历史任务照常计入。`trend` 按任务**首次**进入 `done` 的事件日期累计，窗口前的完成量与没有事件的历史数据计入起点，因此末点与 `completed` 一致；任务被回退后再完成不会重复计数。基准日默认为服务器本地日期，可用 `days` 调整窗口（1—90 天）。

`stats/tasks` 的下钻维度为 `status`、`assignee`、`requirement`、`completed_on`、`overdue`、`due_soon`。`assignee` 取用户编号或 `unassigned`，`requirement` 取需求编号或 `none`，`completed_on` 取 `YYYY-MM-DD`；`limit` 默认 100、最大 200，返回 `items`、`total`、`truncated`。维度或取值不合法返回 400，项目不存在返回 404。

`risks` 只返回未完成、有截止日且剩余天数不超过 3 天的任务，按截止日升序，`limit` 最大 50；每项含 `days_left` 与 `level`（`overdue` 或 `due_soon`）。该清单是查询结果，不写回任务。

`audit` 读取 `audit_events`，按 `id` 倒序，`entity_type` 只接受 `task`、`requirement`、`membership`，`limit` 最大 100。每条记录含 `action`、`field`、`from_value`、`to_value`、`actor_email` 与 `entity_label`。事件在写事务内生成：任务创建记 `create`，字段变化按状态、标题、关联需求、负责人、截止日、排序逐条记 `update`；需求归档/恢复记 `archive`/`restore`；成员加入、改派角色、移出分别记 `create`、`update`、`remove`。事件表只读，服务端不提供修改或删除事件的接口；`actor_user_id` 在用户被删除后置空，但事件保留。

变更只在同一事务内提交：写操作失败会连同事件一起回滚，不会留下“改了但没有记录”的状态。事件不记录任务正文快照，只记录字段前后值；任务被永久删除的能力当前不存在，因此事件可长期追溯。

## 数据库与验证（v3 补充）

结构版本提升到 3，新增 `audit_events` 表与两个索引；打开包含用户数据的 v2 数据库时，会先生成 `.pre-audit.bak` 快照再建表，失败回滚。健康检查工具同步要求该表存在，并新增“任务事件跨项目”检查。统计接口的自动化覆盖见 `tests/test_audit_and_stats.py` 与 `tests/test_stats_api.py`；浏览器回归见 `scripts/browser_insights.mjs`。
