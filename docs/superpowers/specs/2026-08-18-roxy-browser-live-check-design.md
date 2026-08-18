# Roxy 真实浏览器查活设计

## 1. 背景

当前账号查活由 `core/account_liveness.py` 使用 `curl_cffi` 协议会话完成。流程依次请求
ChatGPT Providers、CSRF、Signin，随后进入 OpenAI OAuth 邮箱 OTP 登录，完成 callback 后
读取 `/api/auth/session` 并写回最新 `accessToken`。

近期查活日志表明，多数失败发生在提交邮箱之前的 Providers 阶段，错误为 Cloudflare
HTTP 403。这类错误只说明当前协议会话或网络出口未被放行，不能证明账号失效。真实浏览器
访问同一接口可能成功，因为浏览器具备完整 JavaScript、Cookie 和 Cloudflare 会话状态。

项目已有注册后设置密码后台任务。该任务能够打开账号保存的 Roxy 环境，在真实浏览器中
执行重新认证和邮箱 OTP，并具备 profile 恢复、队列、日志、失败分类和延迟重试能力。
浏览器查活应复用这些基础能力，但不得复用设置密码业务动作，因为查活不能修改密码。

## 2. 目标

新增显式的 `browser` 查活模式，通过 Roxy 真实浏览器登录目标账号、读取
`/api/auth/session`、校验账号身份并刷新数据库中的 token。

具体目标：

1. 保留现有 `protocol` 查活，保持接口和已有用户行为兼容。
2. 新增 `browser` 查活，不在第一版实现自动从协议模式降级到浏览器模式。
3. 浏览器已有正确登录态时直接读取 session，避免不必要的 OTP。
4. 没有登录态时使用目标邮箱完成一次性验证码登录。
5. token 写回前严格验证 session 邮箱和已有账号身份，防止串号。
6. 浏览器查活不修改密码、不创建新账号、不补全注册资料。
7. 网络、Roxy、OTP 等基础设施错误不得标记为废号。
8. 浏览器任务默认单并发，失败按明确分类有限退避。
9. 日志不得包含 OTP、token、Cookie、OAuth code 或代理凭据。

## 3. 非目标

本次设计不包含：

- 自动在 `protocol`、`browser`、`direct` 和不同代理之间轮换；
- 通过高频重试、轮换 IP 或伪造浏览器状态规避第三方风控；
- 修改设置密码流程及其状态语义；
- 使用浏览器查活自动修复未完成注册的账号；
- 清空或覆盖失败账号原有的可用 token；
- 把套餐查询、Codex OAuth 或注册流程迁移到新的查活队列。

## 4. 模式与兼容性

查活 API 增加 `mode`：

```text
protocol  现有 curl_cffi 协议查活
browser   新增 Roxy 真实浏览器查活
```

未传 `mode` 时默认 `protocol`，保证旧 WebUI、旧 API 调用和已有测试不改变行为。

第一版不提供 `auto`。自动降级会在协议失败后意外启动大量浏览器，也会让同一任务切换网络
出口，导致诊断困难。若后续需要 `auto`，必须另行设计任务预算、降级条件和路由稳定性。

两种模式共用现有账号查活状态：

```text
queued -> running -> live | failed | deactivated
```

同一账号只能存在一个查活任务。`protocol` 与 `browser` 必须共同受
`db.claim_account_live_check()` 的原子占用约束。

## 5. 总体架构

浏览器查活拆分为三层：

1. `live_check_service`：接收任务、原子占用账号、按 mode 分发、统一写回结果；
2. `roxy_live_check`：管理 Roxy profile、浏览器生命周期和现有账号登录；
3. `db`：保存统一查活状态、最新 token、浏览器后端和诊断字段。

数据流：

```text
WebUI/API
  -> enqueue_account_live_check(mode="browser")
  -> browser executor
  -> open saved profile or create temporary profile
  -> inspect current ChatGPT session
  -> login with email OTP when session is absent
  -> fetch and validate /api/auth/session
  -> db.update_account_liveness(result)
  -> close browser and clean temporary profile according to policy
```

## 6. 组件设计

### 6.1 `core/roxy_live_check.py`

新增独立模块，避免继续扩张 `core/roxy_registration.py`，也避免把查活逻辑放入设置密码
任务服务。

公开接口：

```python
def check_account_liveness_with_roxy(
    account_id: int,
    email: str,
    *,
    progress_callback=None,
) -> dict:
    """在 Roxy 浏览器中验证账号并返回统一查活结果。"""
```

成功结果：

```python
{
    "ok": True,
    "status": "live",
    "backend": "browser",
    "failure_kind": None,
    "access_token": "<redacted outside persistence>",
    "session": {...},
    "checked_at": "2026-08-18T12:00:00",
    "profile_id": "profile-id",
    "profile_source": "saved" | "temporary",
    "proxy_used": "<masked proxy or None>",
}
```

失败结果：

```python
{
    "ok": False,
    "status": "failed" | "deactivated",
    "backend": "browser",
    "failure_kind": "otp_timeout",
    "checked_at": "2026-08-18T12:00:00",
    "error": "等待邮箱验证码超时",
}
```

模块内部职责：

- 读取账号保存的 Roxy profile；
- 打开 profile，失效时最多创建一个临时 profile；
- 构造 Selenium driver；
- 读取已有 ChatGPT session；
- 必要时执行邮箱 OTP 登录；
- 验证 session 身份和 token；
- 分类异常并生成统一结果；
- 关闭 driver；
- 关闭历史 profile，按配置清理临时 profile。

### 6.2 浏览器登录能力

从现有 Roxy 注册代码中抽取可独立测试的现有账号登录接口：

```python
def login_existing_account_with_otp(
    driver,
    email: str,
    *,
    progress_callback=None,
) -> dict:
    """登录已注册账号并返回 ChatGPT session；不得执行注册或设置密码。"""
```

该接口复用现有页面工具、OTP 输入、页面等待和 session 读取能力，但业务边界必须明确：

- 允许进入 ChatGPT 登录页和 OpenAI 邮箱验证页；
- 允许点击一次性验证码登录入口；
- 允许提交目标邮箱的新 OTP；
- 允许等待 OAuth callback 并打开 ChatGPT；
- 不允许填写 `create-account/password`；
- 不允许填写 `about-you`、姓名、生日或注册 consent；
- 不允许调用 `_run_roxy_password_setup()`；
- 进入注册资料页时返回 `account_incomplete`。

### 6.3 session 读取

复用或抽取以下现有能力：

- `_has_access_token(driver)`；
- `_read_chatgpt_session_once(driver)`；
- `_fetch_chatgpt_session(driver)`；
- `_switch_to_chatgpt_window_if_any(driver)`。

抽取后应放入职责清晰的共享模块，或者在不扩大改动面的前提下由
`roxy_live_check` 导入。实施阶段优先采用最小抽取，不重构无关注册代码。

## 7. profile 生命周期

### 7.1 历史 profile

从账号 `extra_json.roxybrowser.profile_id` 读取历史 profile。

- 可打开：标记 `profile_source=saved` 并复用；
- 打开失败且属于 profile 不存在、窗口数据失效等已知错误：创建一个临时 profile；
- 其他 Roxy API 错误：返回 `browser_open_failed`，不得继续创建多个环境；
- 任务结束后关闭历史 profile，但不得删除。

### 7.2 临时 profile

账号没有 profile，或历史 profile 明确失效时，创建一个临时 profile。

- 每个任务最多创建一个；
- 代理由 Roxy profile 创建配置决定，不使用 `PLAN_CHECK_PROXY`；
- 成功后按配置关闭并清理；
- 失败后默认也清理，防止临时环境累积；
- 清理失败只记录基础设施错误，不改变已经取得的成功 token。

### 7.3 profile 串号保护

打开历史 profile 后首先读取 `/api/auth/session`。

- session 无 token：进入目标邮箱 OTP 登录；
- session 邮箱等于目标邮箱：直接验证并成功；
- session 邮箱不等于目标邮箱：返回 `profile_account_mismatch`，不写回 token；
- 第一版不自动退出错误账号并清 Cookie，因为这会改变用户保存的历史 profile 状态。

## 8. 浏览器查活状态机

### 8.1 已登录快捷路径

1. 打开 `https://chatgpt.com/`；
2. 浏览器页面内以 `credentials: include` 请求 `/api/auth/session`；
3. 有 token 时校验邮箱、user id 和 token claims；
4. 校验通过后直接返回成功，不触发 OTP。

### 8.2 OTP 登录路径

1. 打开 `https://chatgpt.com/auth/login`；
2. 检测已有登录态，避免重复操作；
3. 进入邮箱登录并填写目标邮箱；
4. 选择一次性验证码登录；
5. 在触发验证码之前调用 `capture_otp_baseline(email)`；
6. 记录 `otp_after_ts`；
7. 调用 `wait_for_otp(email, after_ts=..., otp_baseline=...)`；
8. 填写 OTP 并提交；
9. 等待 OAuth callback 或主动回到 ChatGPT；
10. 调用 `_fetch_chatgpt_session()` 取得 token；
11. 执行身份校验并返回结果。

OTP 无效时可在当前浏览器会话内重发，最多三次。每次重发前重新抓取 baseline，重发后
更新 `otp_after_ts`。验证码值不得写入日志。

## 9. token 与身份校验

任何 token 写回前都必须完成以下校验：

1. `session.accessToken` 是非空字符串；
2. `session.user.email` 与目标邮箱忽略大小写后相等；
3. JWT payload 中如存在邮箱，也必须与目标邮箱相等；
4. JWT payload 中如存在 ChatGPT user id，必须与 session user id 一致；
5. 数据库已有 `user_id` 且与新 session user id 不同，返回 `account_identity_mismatch`；
6. JWT `exp` 如存在且已经过期，返回 `session_expired`。

失败时不得覆盖或清空数据库中的旧 `access_token`。

成功时继续调用 `db.update_account_liveness()`，并扩展保存：

```text
live_check_backend=browser
live_check_failure_kind=None
live_check_profile_id
live_check_profile_source
live_check_proxy_used
```

原有 `access_token`、`user_id`、`user_name`、`plan_type`、`expires_at` 写回语义保持不变。

## 10. 错误分类

浏览器查活使用以下 `failure_kind`：

| failure_kind | 含义 | 状态 | 自动重试 |
|---|---|---|---|
| `network_unavailable` | 页面加载、连接或远端临时失败 | `failed` | 是 |
| `browser_open_failed` | Roxy/profile/driver 启动失败 | `failed` | 是 |
| `otp_timeout` | 邮件服务未在期限内返回 OTP | `failed` | 是 |
| `otp_invalid` | OTP 无效或过期 | `failed` | 先在会话内重发 |
| `session_missing` | 登录后没有 accessToken | `failed` | 是 |
| `session_expired` | 新取得 token 已过期 | `failed` | 是 |
| `profile_account_mismatch` | profile 已登录其他邮箱 | `failed` | 否 |
| `account_identity_mismatch` | user id 与数据库身份冲突 | `failed` | 否 |
| `account_incomplete` | 登录后进入注册资料页 | `failed` | 否 |
| `account_unusable` | 明确停用、删除或封禁 | `deactivated` | 否 |
| `unknown` | 未分类异常 | `failed` | 有限重试 |

只有 `account_unusable` 可以将账号标记为 `deactivated`。Cloudflare 页面、超时、OTP、浏览器
启动和 session 缺失均不得判定废号。

## 11. 队列与重试

浏览器查活使用独立执行器，避免占用协议查活 worker，也避免同时启动多个 Roxy 窗口。

新增配置：

```env
LIVE_CHECK_BROWSER_WORKERS=1
LIVE_CHECK_BROWSER_QUEUE_LIMIT=100
LIVE_CHECK_BROWSER_MAX_ATTEMPTS=3
LIVE_CHECK_BROWSER_RETRY_DELAYS=15,60,180
LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE=True
```

约束：

- worker 默认且最低为 1；
- 第一版默认最大执行三次；
- 延迟重试期间不占用 worker 和队列 semaphore；
- 同一账号的重试继续使用同一查活原子占用状态；
- 明确不可重试错误立即结束；
- WebUI 进程重启时，已有 `queued/running` 任务沿用现有恢复逻辑标记为失败；
- 重试不得在一次任务内轮换 `protocol` 与 `browser` 后端。

## 12. API 与 WebUI

现有查活 API 请求增加：

```json
{
  "account_ids": [119],
  "mode": "browser"
}
```

合法 mode 仅为 `protocol` 和 `browser`。非法值返回 HTTP 400。未传时使用 `protocol`。

账号页面提供两个明确动作：

```text
协议查活
浏览器查活
```

批量查活弹窗提供查活方式选择，默认协议查活。浏览器批量查活必须显示单并发提示，但仍可
一次入队多个账号。

账号列表和日志弹窗展示：

- 查活状态；
- 查活后端；
- 最近查活时间；
- failure kind 的中文说明；
- 脱敏的 profile 和代理摘要。

协议与浏览器查活继续使用同一个日志 API 和同一个账号日志文件：

```text
注册日志/live-check-<email>.log
```

浏览器日志统一使用 `[浏览器查活]` 前缀。

## 13. 日志与敏感信息

禁止记录：

- OTP 明文；
- access token 或 token 片段；
- OAuth callback URL 中的 code 和 state；
- Cookie 值；
- 代理用户名和密码；
- 账号设置密码。

允许记录：

- account id 和邮箱；
- profile id；
- profile 来源；
- 脱敏代理地址；
- 当前页面 host 和 path；
- OTP attempt，不含验证码值；
- failure kind 和脱敏错误；
- session 邮箱是否匹配，不打印 token。

现有 Roxy 辅助函数如会记录完整 callback URL，浏览器查活调用路径必须先增加 URL 查询参数
脱敏，避免把 OAuth code 带入查活日志。

## 14. 数据库兼容

账号数据仍保存在现有 JSON/文本导出体系中，不引入数据库迁移工具。

新增字段均为可选字段，旧账号缺失时按以下默认值处理：

```text
live_check_backend=protocol
live_check_failure_kind=None
live_check_profile_id=None
live_check_profile_source=None
```

查活失败只更新查活诊断字段，不清空 token、套餐、用户身份和设置密码状态。查活成功更新
token 后继续调用现有导出逻辑，保证 `注册成功的token.txt` 与账号页一致。

## 15. 文件边界

计划新增：

- `core/roxy_live_check.py`：浏览器查活核心；
- `tests/test_roxy_live_check.py`：浏览器认证和身份保护测试；
- `tests/test_live_check_browser_service.py`：队列、重试和写回测试。

计划修改：

- `core/live_check_service.py`：mode 校验与后端分发；
- `core/roxy_registration.py`：最小抽取浏览器 session/OTP 登录能力；
- `core/db.py`：保存 backend、failure kind 和 profile 诊断字段；
- `config/roxybrowser.py`：浏览器查活配置；
- `config/__init__.py`：导出新增配置；
- `webui/app.py`：接收和返回 mode；
- `webui/config_editor.py`：配置编辑项；
- `webui/templates/index.html`：协议/浏览器查活入口和状态展示；
- `tests/test_webui_account_features.py`：WebUI/API 回归；
- 现有查活测试：保证默认协议模式兼容。

## 16. 测试策略

### 16.1 浏览器核心单元测试

- 已登录且邮箱一致时不触发 OTP，直接返回 token；
- 已登录邮箱不一致时返回 `profile_account_mismatch`，不写回 token；
- session user id 与数据库冲突时拒绝写回；
- 无登录态时完成 OTP 登录并取得 session；
- OTP 超时和无效具有正确 failure kind；
- 进入 about-you 页面返回 `account_incomplete`；
- 明确账号停用返回 `deactivated`；
- 失败结果不包含 token、OTP、Cookie 和 callback code。

### 16.2 profile 生命周期测试

- 历史 profile 可用时复用且不删除；
- 历史 profile 明确失效时只创建一个临时 profile；
- 其他 Roxy 错误不反复创建环境；
- 临时 profile 按配置清理；
- driver 在 profile cleanup 之前关闭；
- 成功写回不因 cleanup 失败改为失败。

### 16.3 服务与并发测试

- 未传 mode 默认调用协议后端；
- `mode=browser` 只调用浏览器后端；
- 非法 mode 被拒绝；
- 同一账号不能同时运行两种查活；
- 浏览器执行器默认只有一个 worker；
- 可重试错误按配置延迟且不占用 worker；
- 不可重试错误不重新入队；
- 成功写回最新 token，失败保留旧 token。

### 16.4 WebUI 测试

- 单账号和批量接口正确传递 mode；
- 账号页提供协议和浏览器两个入口；
- 浏览器队列状态、backend 和 failure kind 正确显示；
- 原有协议查活按钮和 API 保持兼容。

### 16.5 人工验收

选择两个已知正常账号逐个验证：

1. 一个保存的 Roxy profile 已登录目标账号，确认无需 OTP 即可刷新 token；
2. 一个新临时 profile，确认邮箱 OTP 登录后刷新 token；
3. 使用登录了其他账号的 profile，确认系统拒绝串号写回；
4. 检查日志中不存在 OTP、token、Cookie 和 OAuth code；
5. 确认浏览器查活失败不会清空旧 token 或标记废号。

## 17. 验收标准

1. `protocol` 查活保持现有默认行为。
2. `browser` 查活可通过 Roxy 真实浏览器读取或重新建立目标账号登录态。
3. 正确 session 的最新 token 和账号资料写回现有账号记录。
4. 错误邮箱或错误 user id 的 session 绝不写回。
5. 浏览器查活不修改密码、不创建新账号、不填写资料页。
6. 历史 profile 不删除，临时 profile 按配置清理。
7. 浏览器任务默认单并发，可重试错误有限退避。
8. 基础设施错误不会被标记为账号废号。
9. 查活失败不清空旧 token。
10. 日志和 API 响应不泄漏敏感认证信息。
11. 新增测试和现有查活、Roxy、设置密码、WebUI 回归测试全部通过。
