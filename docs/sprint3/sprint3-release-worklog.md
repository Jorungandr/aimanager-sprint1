# Sprint 3 发布工作记录

记录本轮实际发生的开发、验证与发布动作，供交付物 3、4、5 与发布检查清单取材。时间为本机时间，命令输出以实际运行结果为准。

## 一、起点与范围

- 起点基线：`c949271`（Sprint 2 合入后的 `main`），单元测试 138 项通过。
- 本轮分支：`feature/sprint3-analytics-and-audit`。
- 入选故事：US-31、US-36、US-40、US-32、US-34、US-38、US-12，共 7 条 17 点（按 Sprint 2 实测 7 点重估）。
- 顺延：US-41、US-02、US-33、US-39、US-42，理由见 `docs/release-notes.md`。
- 技术债：TD-S2-01 清偿（`audit_events`）；TD-S2-03 由三套浏览器脚本覆盖；TD-S2-02、TD-S2-04 登记为发布后偿还。

## 二、实现要点

| 层 | 改动 |
| --- | --- |
| 数据库 `database.py` | 结构版本 2→3；新增 `audit_events` 表与按项目、按对象两组索引；v2 库升级前生成 `.pre-audit.bak` |
| 业务 `service.py` | 写事务内登记事件；新增 `project_stats`、`project_risks`、`drilldown_tasks`、`list_audit` |
| 接口 `server.py` | 新增四个只读入口；写操作传入操作人；统一查询参数校验 |
| 页面 `index.html` | 新增“数据概览”标签与面板 |
| 交互 `app.js` | 指标卡、状态柱形、趋势折线、负责人条图、需求完成率、风险清单、变更记录与下钻明细 |
| 样式 `styles.css` | 概览卡片、图区与表格样式；触控目标 44px 覆盖紧凑按钮 |
| 工具 `scripts/check_database.py` | 必需表加入 `audit_events`；新增任务事件跨项目检查 |

统计口径：`total` 为全部任务；`completed` 为 `done`；`overdue` 为未完成且截止日早于基准日；`due_soon` 为未完成且 3 天内到期；趋势按任务首次进入 `done` 的事件日期累计，窗口前完成量与无事件的历史数据计入起点。

## 三、验证

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 单元与接口 | `python -m unittest discover -s tests -v` | 159 项通过，122.8 秒（2026-10-24） |
| 新增服务层测试 | `tests/test_audit_and_stats.py` | 15 项通过 |
| 新增接口测试 | `tests/test_stats_api.py` | 6 项通过 |
| 浏览器 smoke | `node scripts/browser_smoke.mjs` | 通过 |
| 浏览器验收 | `node scripts/browser_acceptance.mjs` | 通过 |
| 浏览器概览 | `node scripts/browser_insights.mjs` | 8 项检查通过 |

浏览器概览检查：指标与接口一致、柱形下钻、趋势点下钻、键盘回车下钻、需求下钻与变更筛选、只读成员可看不可改、1440/768/390 无页面横向溢出、窄屏图表随卡片缩放。

## 四、发布动作

- 分支推送、PR、合入 `main`、打 `v1.0.0` 标签，具体提交号与 CI 运行编号在完成后补记于本节。

## 五、未解决与限制

- 未在 Safari、Firefox、真实手机设备上执行回归。
- 趋势按服务器本地日期分桶，跨时区部署需统一时区。
- 下钻单次最多 200 条。
- 事件只记录字段前后值，不保存任务正文快照。
- 会话仍在进程内，重启失效（TD-S2-04）。
