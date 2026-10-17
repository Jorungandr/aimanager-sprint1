# 数据库升级与恢复可靠性 Implementation Plan

**Goal:** 升级失败不留下部分结构修改，备份可在隔离副本中恢复并重新升级。

**Architecture:** 保留 SQLite 和现有 Store 入口。升级前按原库结构生成兼容命名的备份；建表、增量迁移、权限修正、索引及版本标记在一个显式事务中提交。不开设自动覆盖正式数据库的恢复入口。

**Tech Stack:** Python sqlite3、unittest、tempfile。

## 执行与验收

1. 在 tests/test_requirements_service.py 添加迁移中途失败回滚、备份失败不修改原库、恢复副本后重新升级的测试；先运行确认回滚测试暴露现有问题。
2. 修改 database.py 的备份顺序和事务边界，保持已有导入、版本号和备份文件名兼容。
3. 更新 docs/business-api.md，说明停服恢复步骤和备份限制；运行全部后端测试及 git diff --check。

所有故障和恢复演练只使用临时目录；不修改真实 data/aimanager.db、队友文件和 SPRINT2_EVIDENCE.txt。
