# 数据库拆分实施计划

**Goal:** 将专项测试纳入CI，再分离数据库初始化，保留业务入口和数据兼容性。
**Architecture:** 第一步新增专项测试和对接说明，不改现有队友文件。第二步新增database.py管理连接、建表、备份和迁移，service.py继续导出Store、AppError与SCHEMA_VERSION。
**Tech Stack:** Python标准库、SQLite、unittest。

1. 新增tests/test_requirements_service.py和docs/business-api.md，运行全量42项后端测试，单独提交并推送个人分支。
2. 新增database.py，把初始化代码原义迁移；版本不支持错误在service.py映射为原有AppError409，连接异常时关闭资源。
3. 重跑全量42项，增加初始化失败后连接可释放的回归，检查旧库、内存库及旧导入方式；检查diff不涉及server.py/app.js。
4. 第二次提交推送feature/liuhaotian-business，等待该提交后端和浏览器CI通过，不创建PR、不合main。
