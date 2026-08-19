# 已有账号手动补设 2FA 实施计划

> **For agentic workers:** 按任务顺序实施，使用复选框（`- [ ]`）记录进度。每个任务先补测试、再做最小实现、最后运行对应测试；不得覆盖工作区内与本计划无关的修改。

**Goal:** 在账号管理页为尚未保存 TOTP Secret 的已有账号提供单个和批量“补设 2FA”操作；任务通过 RoxyBrowser 重新登录账号，自动读取邮箱 OTP，完成 OpenAI TOTP enroll/activate，并安全写回账号状态和 Secret。

**Architecture:** 新增浏览器内 2FA 协议模块和独立后台任务队列。后台任务打开账号历史 Roxy Profile（失效时创建临时 Profile），复用已有 `login_existing_account_with_otp()` 建立真实浏览器登录态，再在 `chatgpt.com`/`auth.openai.com` 页面上下文中完成二次邮箱重认证、TOTP enroll 和 activate。账号 JSON 继续作为状态真源；普通列表只暴露 `totp_enabled` 和任务状态，不暴露 accessToken、邮箱 OTP、TOTP 动态码或 Secret。

**Tech Stack:** Python 3、Flask、Selenium、RoxyBrowser HTTP API、`pyotp`、现有 JSON 文件持久化、原生 HTML/CSS/JavaScript、pytest/unittest。

**Implementation Status (2026-08-18):** 功能代码、后台队列、WebUI、配置、文档和自动化测试已实现；专项与相关回归测试通过，全量测试未新增失败。真实账号检查点尚未执行，需用户明确指定测试账号后进行。

## Global Constraints

- “手动补设”表示用户在 WebUI 主动点击触发；首版邮箱 OTP 仍由现有邮箱提供方自动读取，不在 HTTP 请求中传递尚未产生的验证码。
- 已有账号不能只使用数据库中的 `access_token` 补设 2FA；必须建立包含 Cookie、CSRF、设备上下文和一致出口 IP 的有效登录会话。
- 登录已有账号通常消耗第一封邮箱 OTP，2FA 重认证通常消耗第二封邮箱 OTP；两个阶段必须分别采集 OTP baseline，禁止复用旧码。
- 只有 activate 明确返回成功后才能写入 `totp_secret`；enroll 成功但 activate 结果不明确时不得从头自动 enroll。
- 普通 `/api/accounts` 响应、任务状态接口、日志、异常和 Toast 均不得包含 accessToken、邮箱 OTP、TOTP 动态码、TOTP Secret 或 Roxy API Token。
- 已存在 `totp_secret` 的账号直接跳过；平台已启用 MFA 但本地没有 Secret 时标记 `already_enabled_external`，不得声称可以恢复原 Secret。
- 同一账号不可同时执行设置密码、浏览器查活和补设 2FA 等会修改/占用登录态的浏览器任务。
- 默认单线程执行 2FA 任务，避免同邮箱、同 Profile 或 Roxy 窗口相互干扰。
- 历史批次归档不回写；账号数据库和对应邮箱池记录作为补设后的最新状态来源。
- 当前全量测试基线为 333 passed、3 failed；计划实施后专属测试必须全部通过，全量测试不得新增失败。

---

### Task 1: 固化安全契约并清理现有 2FA 日志泄露

**Files:**
- Modify: `core/account_export.py`
- Create: `tests/test_twofa_security.py`

**Interfaces:**
- Consumes: 现有协议注册路径的 `_validate_reauth_otp()`、`_activate_totp()` 和 `setup_2fa()`。
- Produces: 统一的 2FA 敏感值脱敏约束，供协议路径和 Roxy 补设路径共同遵守。

- [ ] **Step 1: 写失败测试**

测试模拟 OTP、TOTP 和 Secret，捕获日志后断言以下值均不存在：

```python
email_otp = "123456"
totp_code = "654321"
secret = "JBSWY3DPEHPK3PXP"
```

同时断言日志仍包含可诊断的阶段名称，如 `重认证 OTP`、`enroll`、`activate`。

- [ ] **Step 2: 运行测试确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_twofa_security.py -q`

Expected: FAIL，因为现有实现会记录完整邮箱 OTP 和 TOTP 动态码。

- [ ] **Step 3: 实现统一脱敏**

在 `core/account_export.py` 中：

1. 把 `提交重认证 OTP: {code}` 改成不含验证码的阶段日志。
2. 把 `激活 enrollment, code={totp_code}` 改成不含动态码的阶段日志。
3. 错误响应进入日志前经过统一截断和六位数字脱敏。
4. Secret 日志最多显示固定掩码，不记录首尾片段；成功日志只写“Secret 已生成并保存”。

- [ ] **Step 4: 验证测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_twofa_security.py -q`

Expected: PASS。

### Task 2: 提取可复用的 Roxy 账号任务 Profile 管理

**Files:**
- Create: `core/roxy_account_task.py`
- Modify: `core/password_setup_task_service.py`
- Modify: `tests/test_password_setup_task_service.py`
- Create: `tests/test_roxy_account_task.py`

**Interfaces:**
- Consumes: 账号 `extra_json.roxybrowser.profile_id`、`RoxyBrowserClient`。
- Produces: `profile_id_for_account()`、`open_account_profile_with_recovery()` 和统一 cleanup 语义。

- [ ] **Step 1: 为现有行为补契约测试**

覆盖：

- 有历史 Profile 时优先打开。
- Profile 404/502/503 或已不存在时创建临时 Profile。
- 新建 Profile 标记 `created_by_run=True`，任务结束后按配置关闭/删除。
- 非可恢复错误直接抛出。

- [ ] **Step 2: 创建共享模块**

把 `password_setup_task_service.py` 中 `_profile_id()`、`_is_stale_profile_open_error()`、`_open_profile_with_recovery()` 的通用部分移入 `core/roxy_account_task.py`，日志通过 callback 注入，避免共享模块依赖“设置密码”文案。

- [ ] **Step 3: 让设置密码任务改用共享实现**

不得改变设置密码队列、重试、Profile 清理和日志的现有外部行为。

- [ ] **Step 4: 回归测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_roxy_account_task.py tests/test_password_setup_task_service.py -q`

Expected: PASS。

### Task 3: 实现浏览器内的 2FA 重认证、enroll 与 activate

**Files:**
- Create: `core/roxy_twofa.py`
- Create: `tests/test_roxy_twofa.py`
- Reuse: `core/roxy_registration.py`

**Interfaces:**
- Consumes: 已登录 Selenium driver、账号邮箱、现有 OTP 页面辅助函数。
- Produces: `setup_existing_account_2fa(driver, email, progress_callback=None) -> dict`。

- [ ] **Step 1: 定义返回契约和异常类型**

```python
class TwoFAAlreadyEnabledExternal(RuntimeError): ...
class TwoFAEnrollmentUncertain(RuntimeError): ...

def setup_existing_account_2fa(driver, email, *, progress_callback=None) -> dict:
    return {
        "ok": True,
        "totp_secret": "...",
        "completed_at": "2026-08-18T12:00:00",
    }
```

异常对象只允许携带脱敏阶段、HTTP 状态和可重试标记，不得携带请求 body、Authorization 或 Secret。

- [ ] **Step 2: 写浏览器请求失败测试**

使用 fake driver 模拟 `execute_async_script()`，覆盖：

- CSRF 成功并返回 reauth authorize URL。
- authorize 响应缺少 URL。
- enroll 返回 `secret/session_id`。
- activate 返回 `success=true/false`。
- activate 遇到时间窗口边界时，用同一个 `session_id` 重新生成一次 TOTP 后重试。
- 网络结果不明确时抛出 `TwoFAEnrollmentUncertain`，不得再次 enroll。

- [ ] **Step 3: 实现二次邮箱重认证**

在 `chatgpt.com` 页面上下文中依次执行：

```text
GET  /api/auth/csrf
POST /api/auth/signin/openai?connection=password&reauth=password&max_age=0&...
callbackUrl=https://chatgpt.com/?action=enable&factor=totp
```

打开 authorize URL 前立即采集新的 OTP baseline 和 `after_ts`，确保与登录阶段验证码隔离。进入 `auth.openai.com` 后复用现有 `_is_email_verification_page()`、`wait_for_otp()`、`_type_otp()`、`_click_continue()` 和 `_wait_after_email_otp_submit()`，最多尝试三次。

- [ ] **Step 4: 实现 enroll/activate**

回到 `chatgpt.com` 后通过 `_fetch_chatgpt_session()` 获取新 accessToken，并在浏览器页面上下文中调用：

```text
POST /backend-api/accounts/mfa/enroll
POST /backend-api/accounts/mfa/user/activate_enrollment
```

Python 侧使用 `pyotp.TOTP(secret).now()` 生成动态码。Secret 和 session_id 只保留在当前任务内存中；activate 明确成功前不返回 Secret。

- [ ] **Step 5: 测试双 OTP 基线和敏感值保护**

断言登录 OTP baseline 与 2FA reauth baseline 是两次独立采集；测试输出、日志和异常不含任何验证码、Secret 或 token。

- [ ] **Step 6: 运行测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_roxy_twofa.py tests/test_twofa_security.py -q`

Expected: PASS。

### Task 4: 增加账号 2FA 任务状态持久化

**Files:**
- Modify: `core/db.py`
- Create: `tests/test_account_twofa_state.py`

**Interfaces:**
- Consumes: 账号 ID、任务结果、TOTP Secret。
- Produces: 原子 claim/running/requeue/update/recover 函数和列表可消费的状态字段。

- [ ] **Step 1: 写状态机失败测试**

状态字段：

```text
twofa_setup_status            queued | running | success | failed | already_enabled_external
twofa_setup_ok                true | false | null
twofa_setup_phase             login | reauth | enroll | activate | complete
twofa_setup_attempt
twofa_setup_max_attempts
twofa_setup_last_error
twofa_setup_error
twofa_setup_trigger
twofa_setup_queued_at
twofa_setup_started_at
twofa_setup_completed_at
twofa_setup_next_retry_at
```

覆盖：归档账号拒绝 claim、已有 Secret 跳过、queued/running 防重复、成功才写 Secret、失败不覆盖旧 Secret、进程重启把中断任务标记失败。

- [ ] **Step 2: 实现数据库接口**

```python
claim_account_twofa_setup()
mark_account_twofa_setup_running()
update_account_twofa_setup_phase()
requeue_account_twofa_setup()
update_account_twofa_setup()
recover_interrupted_twofa_setups()
```

成功写入时同步执行：

1. 更新 `row["totp_secret"]`。
2. 重算 `copy_line`。
3. 同步对应 Outlook/generic_api 邮箱池记录的 `totp_secret`。
4. 不修改历史批次归档文件。

- [ ] **Step 3: 实现账号级任务互斥**

claim 时检查该账号是否正在执行密码设置、浏览器查活、注册续跑或另一个 2FA 任务；冲突时返回 busy，不覆盖对方状态。

- [ ] **Step 4: 运行测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_account_twofa_state.py tests/test_account_list_query.py -q`

Expected: PASS。

### Task 5: 实现已有账号 2FA 后台任务队列

**Files:**
- Create: `core/twofa_task_service.py`
- Modify: `config/twofa.py`
- Modify: `config/__init__.py`
- Modify: `config/env_loader.py`
- Modify: `webui/config_editor.py`
- Create: `tests/test_twofa_task_service.py`
- Modify: `tests/test_config_defaults.py`

**Interfaces:**
- Consumes: 账号 ID、邮箱池、Roxy Profile、Task 3 浏览器协议实现。
- Produces: `enqueue_account_twofa(account_id, trigger="manual")`、队列快照和按账号日志。

- [ ] **Step 1: 增加配置**

```python
TWOFA_SETUP_WORKERS = 1
TWOFA_SETUP_QUEUE_LIMIT = 100
TWOFA_SETUP_MAX_ATTEMPTS = 3
```

加入 `.env` override 和 WebUI 配置编辑器；默认并发必须为 1。

- [ ] **Step 2: 写任务测试**

覆盖：

- 账号不存在、归档、已有 Secret、外部已启用、busy、队列满。
- 历史 Profile 打开与失效恢复。
- `login_existing_account_with_otp()` 登录后才调用 `setup_existing_account_2fa()`。
- pre-enroll 网络/OTP 错误按 15/60/180 秒退避重新排到队尾。
- enroll 后结果不明确不自动重试。
- cleanup 在成功、失败、取消时都执行。

- [ ] **Step 3: 实现任务执行函数**

```python
def _run_twofa_task(*, account_id: int, email: str) -> dict:
    # mark running
    # open/recover profile
    # build driver
    # login_existing_account_with_otp
    # detect existing MFA
    # setup_existing_account_2fa
    # update DB
    # cleanup
```

登录后如 `session.user.mfa` 已为真但数据库没有 Secret，写回 `already_enabled_external`。不能尝试生成新 Secret，也不能将该状态展示为“本地 Secret 可用”。

- [ ] **Step 4: 增加独立日志**

日志路径：`注册日志/twofa-setup-{safe_email}.log`。日志只记录阶段、尝试次数、脱敏错误和 Profile ID；统一替换六位数字、Bearer token 和疑似 Base32 Secret。

- [ ] **Step 5: 运行测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_twofa_task_service.py tests/test_config_defaults.py -q`

Expected: 新增 2FA 测试全部 PASS；配置默认值测试需隔离本地 `.env`，不得读取用户运行配置作为源码默认值。

### Task 6: 增加 WebUI 单账号与批量调用 API

**Files:**
- Modify: `webui/app.py`
- Modify: `tests/test_webui_account_features.py`
- Create: `tests/test_webui_twofa_setup.py`

**Interfaces:**
- Consumes: `twofa_task_service` 和数据库状态。
- Produces: 单账号入队、批量入队、状态轮询和日志读取 API。

- [ ] **Step 1: 扩展紧凑账号响应**

普通列表只增加非敏感字段：

```text
totp_enabled
twofa_setup_status
twofa_setup_ok
twofa_setup_phase
twofa_setup_attempt
twofa_setup_max_attempts
twofa_setup_error
twofa_setup_next_retry_at
```

不得加入 `totp_secret`、accessToken 或邮箱 OTP。

- [ ] **Step 2: 增加 API 契约测试**

```text
POST /api/accounts/<int:acc_id>/2fa-setup
POST /api/accounts/2fa-setup-bulk
GET  /api/accounts/2fa-setup-status?ids=1,2,3
GET  /api/accounts/2fa-setup-log?email=...
```

返回码：接受入队 `202`、账号不存在 `404`、重复/busy `409`、队列满 `503`、请求格式错误 `400`。

- [ ] **Step 3: 实现单账号和批量接口**

批量请求限制 500 个账号，去重后逐个入队；响应分别列出 `started`、`skipped`、`failed`，不得包含后台 Future 或敏感值。

- [ ] **Step 4: 启动时恢复中断状态**

在 `create_app()` 初始化阶段调用 `db.recover_interrupted_twofa_setups()`，将无法继续的 queued/running 状态标记为失败，提示用户重新提交。

- [ ] **Step 5: 运行测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_webui_twofa_setup.py tests/test_webui_account_features.py -q`

Expected: PASS，响应正文不含测试使用的 OTP、Secret 和 token。

### Task 7: 增加账号页手动补设交互

**Files:**
- Modify: `webui/templates/index.html`
- Modify: `tests/test_webui_jobs.py`

**Interfaces:**
- Consumes: Task 6 API。
- Produces: 单账号按钮、批量按钮、状态展示、日志查看和轮询。

- [ ] **Step 1: 增加静态契约测试**

断言模板包含：

- `data-account-twofa-setup` 单账号按钮。
- `btnTwoFASetupSelectedV2` 批量按钮。
- `twoFASetupQueueStatusV2` 队列摘要。
- 任务状态轮询 endpoint。
- 2FA 日志入口。

- [ ] **Step 2: 实现按钮显示规则**

```javascript
const twofaBusy = ['queued', 'running'].includes(r.twofa_setup_status);
const canSetupTwoFA = !r.totp_enabled && !twofaBusy;
```

状态展示：

- `totp_enabled=true`：已启用。
- `already_enabled_external`：平台已启用，本地无 Secret。
- `queued/running`：排队中/执行中并显示阶段。
- `failed`：失败，可重新提交并查看日志。

- [ ] **Step 3: 实现确认提示**

提交前提示：“任务将打开 RoxyBrowser，并可能向该邮箱发送两封验证码邮件；验证码由后台自动读取。”不要求用户提前填写 OTP。

- [ ] **Step 4: 实现轮询和批量结果 Toast**

轮询仅针对可见或已提交账号；所有任务进入终态后停止轮询，避免永久请求。批量 Toast 显示入队、跳过、失败数量。

- [ ] **Step 5: 运行测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_webui_jobs.py tests/test_webui_twofa_setup.py -q`

Expected: PASS。

### Task 8: 集成验证、文档与真实账号检查点

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: 以上任务涉及的测试文件

- [ ] **Step 1: 更新用户文档**

说明：

1. 账号页如何单个/批量触发补设。
2. 必须配置可用邮箱来源和 RoxyBrowser。
3. 一次任务可能收到两封 OTP 邮件。
4. 外部已启用但本地无 Secret 时不能恢复原 Secret。
5. Secret 的保存与导出安全风险。

- [ ] **Step 2: 运行专属测试集合**

Run:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest `
  tests/test_twofa_security.py `
  tests/test_roxy_account_task.py `
  tests/test_roxy_twofa.py `
  tests/test_account_twofa_state.py `
  tests/test_twofa_task_service.py `
  tests/test_webui_twofa_setup.py `
  tests/test_webui_jobs.py -q
```

Expected: PASS。

- [ ] **Step 3: 运行相关回归测试**

Run:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe -m pytest `
  tests/test_password_setup_task_service.py `
  tests/test_roxy_password_setup.py `
  tests/test_roxy_live_check.py `
  tests/test_webui_account_features.py `
  tests/test_account_list_query.py -q
```

Expected: PASS，不改变设置密码、查活和账号列表脱敏行为。

- [ ] **Step 4: 运行全量测试并对比基线**

Run: `$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe -m pytest -q`

Expected: 不新增失败；现有 3 个基线失败应单独记录或通过隔离 `.env` 修复后归零。

- [ ] **Step 5: 真实账号检查点（需要用户明确选择测试账号）**

只选择一个本地没有 `totp_secret`、邮箱仍可收信的测试账号，执行单账号补设并检查：

1. Roxy Profile 正确打开或自动恢复。
2. 登录 OTP 与 2FA OTP 是两封不同邮件。
3. activate 明确成功。
4. 账号列表显示“已启用”。
5. 按需 Secret 接口能读取 Secret，普通列表和日志不能读取。
6. 使用保存的 Secret 连续跨两个 30 秒窗口生成 TOTP，动态码格式正确。

真实测试会改变外部账号安全设置，不纳入自动测试，也不得在没有用户明确指定账号时执行。

## Definition of Done

- [ ] 已有账号可从 WebUI 单个或批量提交补设 2FA 任务。
- [ ] 后台能重新建立真实 Roxy 登录态，并区分登录 OTP 与 2FA 重认证 OTP。
- [ ] activate 明确成功后才保存 Secret；不确定状态不会重复 enroll。
- [ ] 任务状态、重试、恢复、日志和账号级互斥完整可观测。
- [ ] 普通列表、API、日志和错误不会泄露 OTP、动态码、Secret、token。
- [ ] 已有 Secret 和外部已启用账号均有明确且幂等的处理结果。
- [ ] 专属测试全部通过，相关回归测试通过，全量测试不新增失败。
- [ ] README 和 `.env.example` 已更新，真实账号操作边界明确。
