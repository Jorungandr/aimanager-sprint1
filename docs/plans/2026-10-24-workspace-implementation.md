# Workspace Redesign Implementation Plan

**Goal:** 完成实验四工作台改版并修复核实的数据明细问题，验证后合入 main。

**Architecture:** 保持现有 HTTP/SQLite/原生 JavaScript；改造页面结构与设计变量，复用既有 DOM 标识及权限控制。下钻总数独立计数，前端用请求序号和项目上下文拒绝过期响应。

**Tech Stack:** Python 3.11+、SQLite、HTML/CSS、JavaScript、Playwright。

## 1. 建立验证基线
- 完整后端：`python -m unittest discover -s tests -q`，当前 159 项通过。
- 安装锁定浏览器依赖，保存 1440px / 390px 改前截图，使用独立临时数据库。
- 不修改实验三目录或演示数据。

## 2. 修复统计明细
- 修改 service.py 的 drilldown_tasks，以相同筛选计算 COUNT，truncated 判断真实总数与返回数。
- tests/test_audit_and_stats.py 补充超限、恰好限额、无结果情况，先验证失败再实现。
- 修改 app.js 的 drillTasks：显式请求 200 条，拒绝过期项目/旧序号/已登出请求，保留正确 busy 状态。

## 3. 实现工作台
- 修改 index.html / styles.css / app.js：共享项目工具栏、侧栏、登录插画、统一数据概览网格、紧凑且清晰的看板。
- assets/ 保存插画与统一图标，server.py 仅允许声明的静态资源路径。
- 保留弹窗冲突保护、44px 触控、权限矩阵横向滚动、键盘状态切换及已有表单标识。

## 4. 验证与交付
- 完整后端与 `npm run test:browser`；新增跨视图项目切换及并发明细回归。
- 实际检查 1440/768/390px 页面，确认图片加载、文字对比、焦点及无页面溢出。
- 更新验收说明，提交开发分支、检查远程新提交及 CI，集成 main，不删除分支。
