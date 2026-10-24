# 后端安全审计报告（2026-10-24）

- **审计对象**：`aimanager-sprint1` 后端（`server.py`、`service.py`、`database.py`、`scripts/check_database.py`），基线提交 `8b396fe`
- **审计范围**：仅后端。前端（`index.html`、`app.js`、`styles.css`）未做任何修改，仅作为 XSS 风险面进行了只读核查。
- **方法**：逐行人工审计（认证/会话/CSRF/越权/注入/资源耗尽/信息泄露/日志安全），并核对既有 159 项测试；每项修复补充回归测试。
- **验证结果**：全量测试 164 项通过（原 159 + 新增 5），无回归。

---

## 一、已修复问题（4 项）

### V-1【中危】登录限速可被跨账号“密码喷洒”绕过
- **位置**：`server.py` 登录处理（原第 325–352 行），现抽为 `Handler.throttle_login`（`server.py:251`）
- **问题**：原限速仅按（账号, IP）二元组计数。攻击者对同一 IP 轮换大量账号名尝试弱口令时，每个账号都有独立的 5 次/分钟配额，单 IP 总尝试次数无上限——正好构成撞库/密码喷洒的典型条件。安全计划文档（`docs/plans/2026-10-17-security-hardening.md`）承诺的是“每 IP 每分钟 5 次失败”，实现弱于承诺口径。
- **修复**：新增按 IP 的全局失败计数 `LOGIN_IP_LIMIT = 20` 次/分钟（`server.py:23`）。触发后该 IP 的所有登录（含正确密码）返回 429 + Retry-After，窗口过后自动恢复。原（账号, IP）指数退避逻辑保持不变；被 429 拦截的尝试不计入失败（与原语义一致，避免自我续期）。
- **测试**：`tests/test_http.py::test_login_spraying_across_accounts_is_ip_throttled`、`test_successful_login_does_not_lift_ip_throttle_set_by_other_accounts`（覆盖：喷洒触发 429、正确密码同被拒、窗口恢复、被账号限速挡下的尝试不重复计数）。

### V-2【中危】限速表无上限，可被匿名请求撑爆内存
- **位置**：`Handler._tracked_failure`（`server.py:245`）、`LOGIN_TRACK_LIMIT = 10_000`（`server.py:24`）
- **问题**：`login_failures` 为 `defaultdict(deque)`，键由请求体中任意伪造的邮箱字符串生成。未认证攻击者每分钟可用不同账号名制造大量键，每个键常驻内存且只在下一次登录尝试时才被清理，停止攻击后键永不回收——可造成持续内存增长（拒绝服务）。
- **修复**：限速表键数上限 10,000；插入新键且表已满时淘汰最早的键（Python 字典保持插入序）。淘汰的键其计数归零，攻击者需重新积累该账号的失败次数，权衡可接受。
- **测试**：`tests/test_http.py::test_rate_limit_tables_stay_bounded_under_account_flooding`（用 patch 将上限调至 2 并伪造 5 个账号，断言两表键数均不超限）。

### V-3【低危】日志注入：请求行/路径中的控制字符原样写入日志
- **位置**：`sanitize_log`（`server.py:31`）、`Handler.log_message`（`server.py:100`）、`handle_request` 的两处异常日志（`server.py:349,351`）
- **问题**：`log_message` 直接打印原始请求行，`logging.warning/exception` 直接输出 `self.path`，三者均含用户可控内容。攻击者可通过请求路径注入 `\r\n` 伪造日志条目（破坏审计完整性），或注入 ANSI/ESC 终端转义序列影响查看日志的终端。
- **修复**：新增 `sanitize_log()`，将 `\x00-\x1f`、`\x7f-\x9f` 统一转义为 `\xNN` 文本；三处日志输出点全部经过滤。中文等多字节文本不受影响。
- **测试**：`tests/test_http.py::test_log_sanitizer_escapes_control_characters`（CRLF、ESC、DEL 及中文直通）。

### V-4【低危·加固】缺少跨域隔离响应头（COOP / CORP）
- **位置**：`Handler.end_headers`（`server.py:95-96`）
- **问题**：已有 X-Frame-Options、CSP、Referrer-Policy、nosniff，但缺少 `Cross-Origin-Opener-Policy` 与 `Cross-Origin-Resource-Policy`，未阻断跨窗口关系（window.opener 侧信道）与资源被任意跨站嵌入。
- **修复**：对所有响应统一追加 `COOP: same-origin` 与 `CORP: same-origin`。本应用无任何跨域资源引用，纯同源使用，无功能影响。
- **测试**：`tests/test_http.py::test_responses_carry_cross_origin_isolation_headers`（API 与静态页均校验）。

---

## 二、已核查、未发现问题的方面（正面确认）

| 审计项 | 结论 |
|---|---|
| SQL 注入 | 全部查询参数绑定；动态子句仅由固定常量拼接；`check_database.py` 的 f-string 仅作用于内置字典的固定表名。未发现注入点。 |
| 存储型/反射型 XSS（后端侧） | 后端只输出 JSON；前端唯一 `innerHTML`（app.js:445）为静态表单无插值，其余 60 处均用 `textContent`。未发现注入路径。 |
| 密码存储 | PBKDF2-SHA256、600,000 次迭代（符合 OWASP 2023 建议）、16 字节随机盐；旧哈希（150k）登录时透明升级并以条件更新防竞态；迭代因子上下限校验防畸形库值 DoS。 |
| 认证时序/枚举 | 用户不存在时执行等工作量假哈希；密码比较用 `hmac.compare_digest`；错误文案统一 401。 |
| 会话安全 | Cookie 仅存随机 token，库内只存 SHA-256 摘要；HttpOnly + SameSite=Strict；TTL 2h；每用户并发上限；登录生成新 token（无固定化）。 |
| CSRF | 双提交（Header == Cookie）+ HMAC 签名绑定会话 token + Origin 校验 + Host 白名单，写方法全覆盖。 |
| 越权/IDOR | 所有 `/api/projects/{id}/*` 路由先 `require_permission`；`/api/users` 仅管理员；角色天花板机制（`assignable_roles`、`set_permissions` 的 ceiling 校验）阻断自提权与越权授权；项目创建者不可被成员角色覆盖/移除。 |
| 输入校验 | JSON 严格解析（拒绝重复键、NaN/Infinity、超深嵌套、非 BOD 字符串）；请求体 ≤16KB；拒绝 Transfer-Encoding 与歧义 Content-Length；路径数字段先正则再转换；查询参数白名单+去重。 |
| 路径遍历 | 静态文件走固定白名单映射，不拼接用户输入。 |
| 并发/资源 | 16 连接信号量+过载 503；2 秒套接字超时；拒绝请求的有界排水（1MB/0.2s）；数据库操作串行化于 RLock；慢请求体在锁外读取。 |
| 数据库层 | 迁移前自动快照备份；拒绝高于支持的 schema 版本；外键约束开启；`check_database.py` 全程只读且不外泄 SQLite 错误文本。 |
| 审计链 | `audit_events` 只写不改不删；记录前后值与操作者。 |

---

## 三、遗留风险与建议（未修改，供决策）

1. **`/api/setup` 首次初始化竞态**（信息级）：管理员未初始化期间，本机任何进程/用户都可抢先 `POST /api/setup` 成为管理员。这是“本机单用户教学工具”信任模型的固有约束（服务只绑定 127.0.0.1），多用户主机上部署时需注意尽快完成初始化。建议在 README 强调。
2. **Cookie 无 `Secure` 标志**（信息级）：当前为纯 HTTP 本机服务，加 `Secure` 会导致部分浏览器拒绝回传 cookie。若未来提供 HTTPS 部署形态，应同步为 `sid`/`csrf` cookie 追加 `Secure`。
3. **迁移备份文件**：`data/*.pre-*.bak` 为完整数据库明文副本（含密码哈希），迁移后长期留存。建议发布/交付检查清单中加入“确认备份去留”。
4. **密码策略**：仅长度 8–128，无弱口令黑名单。教学场景可接受。
5. **`do_OPTIONS`/`do_PATCH` 在 Host 校验前返回 405**：无信息泄露，仅与“所有响应先过 Host 白名单”的口径不一致，未改动。
6. **文档口径**：安全计划文档写“请求读取超时 5 秒”，实现为 2 秒（更严格）。属文档滞后，未改代码。

---

## 四、变更清单

| 文件 | 变更 |
|---|---|
| `server.py` | V-1 ~ V-4 全部修复（+72/-21 行） |
| `tests/test_http.py` | 新增 5 项回归测试（+61 行） |
| `docs/security-review-2026-10-24.md` | 本报告 |

**验证**：`python -m pytest tests/ -q` → **164 passed**（含 CI 工作流同一命令）。
