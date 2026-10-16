# 爱管理 Sprint 2 累积增量

本项目以 Sprint 1 的 E01 用户与权限增量为基线，继续实现项目协作链路：创建需求、创建任务、关联需求、指派项目成员、设置截止日，并在“待梳理—待办—进行中—已完成”看板中推进状态。Sprint 1 的邮箱密码登录、角色与权限、项目成员、权限矩阵和权限回收功能继续保留并纳入回归测试。

现有附件未提供实验一完整用户故事地图，因此本轮文档使用 `S2-01`（需求与任务）和 `S2-02`（任务看板）作为可追溯临时编号；提交前应由 PO 映射回实验一 Sprint 2 切片中的原故事编号，功能边界以原 8 步演示链的第 5—6 步为准。

## 运行

需 Python 3.10 或更高版本，使用标准库，无需安装第三方包。在本目录运行：

```text
python server.py
```

浏览器打开 `http://127.0.0.1:8765`。首次打开会显示初始化管理员页面；请自行设置管理员邮箱和密码。数据保存在运行目录中的 `data/aimanager.db`，会话保存在进程内，重启后需要重新登录。

## 验证

```text
python -m unittest discover -s tests -v
```

演示建议：初始化管理员 → 创建“项目经理、成员”角色 → 为项目经理勾选 `project.view`、`members.manage`、`workitems.manage` → 创建项目与账号并加入成员 → 用项目经理账号创建需求和任务、指派负责人及截止日 → 在看板中拖动任务到“进行中”和“已完成” → 用仅有 `project.view` 的成员账号验证可查看但不可新建或移动任务 → 回归演示 Sprint 1 的登录、权限隔离和成员移除。所有账号和密码均使用演示数据，不要使用真实凭证。

## 实现与边界

- 后端：Python 标准库 HTTP 服务、SQLite、PBKDF2 密码散列、随机会话令牌。
- 权限与完整性：Sprint 1 的四项权限继续保留，Sprint 2 新增项目级 `workitems.manage`；读取需求和任务要求 `project.view`，写入和看板变更要求 `workitems.manage`。任务负责人须为当前项目成员，需求须与任务同属一个项目，截止日采用 `YYYY-MM-DD`，状态只允许 `backlog`、`todo`、`doing`、`done`。
- 服务器执行权限校验，浏览器界面隐藏按钮仅用于辅助提示，不作为安全边界。
- 当前为教学原型，监听本机地址，不包含生产部署所需的 TLS、限流、账号找回、审计日志与跨设备会话持久化。
- 自动测试通过只能证明代码级 DoD 的一部分；团队仍须完成非作者审查、现场 AC 验收、截图/录屏和真实会议签认后，才能把故事标记为正式完成。

## 文件

- `server.py`：HTTP 路由与安全边界。
- `service.py`：数据库与业务规则。
- `index.html`、`app.js`、`styles.css`：浏览器界面。
- `tests/test_service.py`、`tests/test_http.py`：业务、越权与回归测试；`.github/workflows/tests.yml`：推送与拉取请求触发的全量回归流水线。

本轮基于远端 `feature/sprint1-permission-hardening`（含 Sprint 1 PR #1 的权限加固和 CI）创建 `codex/sprint2-task-board`。提交历史保留在源码压缩包中；是否合并回 `main` 由团队在非作者审查和 CI 通过后决定。
