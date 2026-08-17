# 注册后设置密码 OTP 隔离与后台续跑实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让注册成功后的密码设置使用独立 OTP 挑战；即时设置失败时保存注册账号，并在注册环境清理完成后自动后台重试。

**Architecture:** GenericAPI 取码改为“搜索截止时间 + 独立 settle 确认窗口”的两阶段状态机，候选以消息 ID/时间戳而不是验证码字符串作为身份。Roxy 注册流程在当前浏览器中先尝试设置密码，失败后返回 handoff 标记；注册线程结束并清理临时 profile 后，由 registration service 把已保存账号交给现有密码设置队列。后台任务复用同一 OTP 挑战逻辑，失效 profile 自动恢复，失败按 15/60/180 秒退避重试。

**Tech Stack:** Python 3.11、pytest、现有 `GenericAPI`/`email_provider`、Selenium + RoxyBrowser、SQLite/JSON 账号存储、`ThreadPoolExecutor`。

## Global Constraints

- 同码的新邮件必须允许使用；只有无法证明为新投递的同码缓存邮件才拒绝。
- 设置密码授权动作之前必须抓取独立 OTP baseline，并在触发动作之前记录 `triggered_at`。
- 首次进入设密验证码页不得立即点击 `Resend email`；仅在超时或页面拒绝后重发。
- 搜索阶段候选在 `OTP_MAX_WAIT` 截止前出现时，必须额外获得完整 `OTP_SETTLE_SECONDS` 确认窗口。
- `OTP_MAX_WAIT=120`、`OTP_POLL_INTERVAL=3`、`OTP_SETTLE_SECONDS=5` 为默认值；配置仍可通过现有环境覆盖机制调整。
- `ROXY_PASSWORD_SETUP_MAX_RETRIES=3` 表示首次后台执行之外追加 3 次重试，总执行次数最多 4 次。
- 注册成功与设置密码状态分离；设置密码失败不得把注册任务改成失败或释放已确认邮箱。
- 后台入队必须发生在 `run_roxy_registration` 的 driver/profile 清理完成之后。
- 不打印目标密码；新增 OTP 日志使用消息身份、时间和脱敏状态，不新增明文验证码日志。
- 每个任务结束时运行对应的聚焦 pytest；所有任务完成后运行全量 pytest。
- 保留工作区内与本功能无关的未提交修改，不执行批量删除或覆盖。

---

### Task 1: GenericAPI 两阶段 OTP 状态机

**Files:**
- Modify: `core/generic_api_mail_client.py:649-970`（`_matches_otp_baseline`、`capture_otp_baseline`、`fetch_latest_otp`）
- Modify: `config/email.py:48-58,157`（默认 OTP 等待参数）
- Test: `tests/test_generic_api_yangyang.py`
- Test: `tests/test_config_defaults.py`

**Interfaces:**
- Preserve `fetch_latest_otp(email, after_ts=None, max_wait=None, poll_interval=None, settle_seconds=None, exclude_codes=None, otp_baseline=None) -> str` so `core/email_provider.py` 不需要改变公共调用方式。
- Add private observation identity helper with exact contract:

```python
def _otp_observation_key(observation: GenericOtpObservation) -> tuple[str, str, str]:
    """返回 message_id、msg_ts、code 的稳定字符串键。"""
```

- Preserve `OtpBaseline` fields `codes`, `message_ids`, `captured_at`.

- [ ] **Step 1: Write failing tests for message identity and settle grace**

在 `tests/test_generic_api_yangyang.py` 增加以下测试。测试使用现有
`FakeSingleResponseSession`，并为时钟引入局部 `FakeClock`，让 `time.time()` 按测试序列前进：

```python
def test_same_code_from_new_message_id_is_accepted():
    baseline = OtpBaseline(frozenset({"119006"}), frozenset({"mail-old"}), 100.0)
    session = FakeSingleResponseSession({
        "found": True,
        "ok": True,
        "message": {"code": "119006", "timestamp": 105.0, "uid": "mail-new"},
    })
    # fetch_latest_otp(..., after_ts=100.0, otp_baseline=baseline) 返回 "119006"

def test_same_code_same_message_stays_rejected():
    baseline = OtpBaseline(frozenset({"119006"}), frozenset({"mail-old"}), 100.0)
    # 响应 uid=mail-old、timestamp=95.0；fetch_latest_otp 应超时而不是返回旧码

def test_candidate_seen_before_search_deadline_gets_full_settle_window():
    # 候选在 search_deadline 前 1 秒出现，time.sleep 不得截断 settle；最终返回候选

def test_unstable_candidate_fails_after_confirmation_hard_limit():
    # 候选在确认期持续变化，达到 max(15, 3 * settle) 后抛出“候选不稳定”错误
```

- [ ] **Step 2: Run the focused tests and verify RED**

运行：

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py -k "same_code or settle or unstable" -q
```

预期：新增测试失败，现有实现会按验证码字符串比较，并在总 deadline 到达时直接抛出
`settle 未完成`。

- [ ] **Step 3: Implement observation identity and two-phase deadlines**

在 `core/generic_api_mail_client.py` 中：

1. 添加 `_otp_observation_key`，优先组合 `message_id`、`msg_ts`、`code`；缺失字段使用空字符串。
2. 在 GenericAPI JSON 分支和 yangyang 分支都保留当前候选的完整 `GenericOtpObservation`，不要只保存 `best_otp`。
3. 把当前单一 `deadline` 拆成：

```python
search_deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
confirm_deadline = None
hard_confirm_deadline = None
```

4. 新候选判定继续调用 `_matches_otp_baseline`；GenericAPI 有消息 ID/时间戳时不使用
   `exclude_codes` 阻止同码新邮件。
5. 以候选 key 或更晚 `msg_ts` 判断“候选更新”，而不是只比较 `code`；更新候选时重置
   `confirm_deadline = now + settle`。
6. 首个候选出现于 `search_deadline` 前时，设置
   `hard_confirm_deadline = now + max(15, 3 * max(settle, 0))`，允许循环继续到
   `confirm_deadline`；确认硬上限到达且候选仍不稳定时抛出明确的 `候选不稳定` 异常。
7. 每次 sleep 使用 `min(interval, remaining_stage_seconds)`，避免跨过确认边界；候选一旦
   settle 完成就返回。
8. 保留现有 GenericAPI 日志前缀；Task 2 再通过消息前缀标识 `purpose=password_setup`。
   不新增验证码明文日志。

在 `config/email.py` 将 `OTP_MAX_WAIT` 默认值从 90 调整为 120；保留 `apply_env_overrides`
中的现有键名和类型。`tests/test_config_defaults.py` 增加断言 `OTP_MAX_WAIT == 120`，不改
用户通过环境变量覆盖的行为。

- [ ] **Step 4: Run the focused tests and verify GREEN**

运行：

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py tests/test_config_defaults.py -q
```

预期：该组测试全部通过；现有顶层 JSON、嵌套 `message`、基线和旧码拒绝测试不回归。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add core/generic_api_mail_client.py config/email.py tests/test_generic_api_yangyang.py tests/test_config_defaults.py
git commit -m "fix: 分离OTP搜索与settle确认窗口"
```

检查点：提交后记录测试结果，确认未暂存其他工作区文件。

### Task 2: 设置密码独立挑战与取消首次立即重发

**Files:**
- Modify: `core/roxy_registration.py:1749-1845`（`_run_roxy_password_setup`）
- Modify: `core/email_provider.py:154-218`（仅在需要时透传挑战参数，不改变非 GenericAPI provider）
- Test: `tests/test_roxy_password_setup.py`
- Test: `tests/test_roxy_registration_otp_retry.py`

**Interfaces:**
- Preserve `_run_roxy_password_setup(driver, email, mode=None, password=None, previous_otp=None, progress_callback=None) -> str`。
- Each OTP wait continues to call `wait_for_otp(email, after_ts=..., otp_baseline=...)`; `exclude_codes` 仅为非 GenericAPI provider 保留。
- Use existing `capture_otp_baseline(email)` and `_click_resend_email_otp(driver, timeout=25)`.

- [ ] **Step 1: Write failing tests for the password setup challenge order**

在 `tests/test_roxy_password_setup.py` 增加：

```python
def test_initial_password_setup_waits_for_auto_sent_otp_without_resend():
    # patch capture_otp_baseline、_fetch_password_setup_authorize_url、_safe_get、
    # _is_email_verification_page=True、wait_for_otp="119006"、页面 accepted；
    # 断言 _click_resend_email_otp 未调用，wait_for_otp 收到 otp_baseline 和 after_ts。

def test_password_setup_same_code_from_new_generic_message_is_not_excluded():
    # resolve_email_source 返回 generic_api，previous_otp="119006"；
    # 断言 wait_for_otp 的 kwargs 不含 exclude_codes，且同码候选被输入页面。

def test_password_setup_resend_refreshes_baseline_before_trigger():
    # 首轮 wait_for_otp 抛出超时，第二次返回新码；
    # 断言第二次 capture_otp_baseline 发生在第二次 _click_resend_email_otp 之前。
```

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_password_setup.py -k "initial_password_setup or same_code or refreshes_baseline" -q
```

预期：现有代码会在首次进入验证码页后立即调用 Resend，且 GenericAPI 的首次等待不带
独立 baseline。

- [ ] **Step 3: Implement per-attempt challenge lifecycle**

修改 `_run_roxy_password_setup`：

1. 在 `_fetch_password_setup_authorize_url` 之前调用 `capture_otp_baseline(email)`，并立即记录
   `otp_after_ts = time.time()`；非 GenericAPI 来源返回 `None` baseline。
2. 打开 authorize URL 并确认进入邮箱验证码页后，直接执行第一次 `wait_for_otp`；删除当前
   “只要有 previous_otp 就先 Resend”的无条件分支。
3. GenericAPI 不把 `previous_otp` 加入 `exclude_codes`；Outlook 等没有可靠消息身份的来源
   继续保留旧码排除。
4. OTP 等待超时或页面明确拒绝时，下一轮开始前按此顺序执行：

```python
otp_baseline = capture_otp_baseline(email)
otp_after_ts = time.time()
_click_resend_email_otp(driver, timeout=25)
code = wait_for_otp(email, after_ts=otp_after_ts, otp_baseline=otp_baseline, ...)
```

5. 每轮调用 `wait_for_otp` 前检查页面是否已经自动进入新密码页；若已进入则跳过重复输入。
6. 日志只输出 `purpose=password_setup`、attempt、baseline 是否存在和触发时间，不输出 OTP 值。

- [ ] **Step 4: Run the password setup regression suite**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py -q
```

预期：新增测试和原有 2FA/页面已跳转/旧 provider 排除测试全部通过。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add core/roxy_registration.py core/email_provider.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py
git commit -m "fix: 隔离注册与设密验证码挑战"
```

检查点：确认首次设密挑战不再自动重复发送邮件。

### Task 3: 注册成功后的设置密码 handoff

**Files:**
- Modify: `core/roxy_registration.py:2427-2534`（注册结果、账号保存和清理边界）
- Modify: `core/registration_service.py:421-500`（注册返回后的后台入队）
- Modify: `core/db.py:1390-1518`（handoff 状态原子更新和字段清理）
- Create: `tests/test_registration_password_handoff.py`
- Modify: `tests/test_roxy_password_setup.py`

**Interfaces:**
- `run_roxy_registration` 成功返回增加内部字段 `password_setup_handoff: bool`；该字段不进入普通账号密钥输出。
- `password_setup_task_service.enqueue_account_password_setup(*, account_id: int, mode: str, password: str, trigger: str = "manual") -> dict` 是唯一入队入口。
- `db.claim_account_password_setup(...) -> bool` 继续保证同一账号不会并发领取；handoff 入队失败写回 `failed` 但不改变注册任务成功状态。

- [ ] **Step 1: Write failing handoff tests**

创建 `tests/test_registration_password_handoff.py`，覆盖以下可执行断言：

```python
def test_registration_success_with_password_handoff_queues_after_runner_returns():
    # patch main.run_registration 返回 {success: True, account_id: 42,
    # password_setup_handoff: True, email: "user@example.com"}；
    # patch enqueue_account_password_setup；运行 registration_service 的单任务入口；
    # 断言先完成 run_registration，再调用 enqueue，且任务最终 status=success。

def test_password_handoff_enqueue_failure_keeps_registration_success():
    # enqueue 返回 accepted=False；断言 job status 仍为 success，日志/账号状态记录
    # 设置密码入队失败，而不是把 job 改为 failed。
```

在 `tests/test_roxy_password_setup.py` 增加：

```python
def test_inline_password_setup_failure_returns_handoff_flag_and_saves_account():
    # 模拟 accessToken 已取得、_run_password_setup_with_gate 抛出 GenericApiMailError；
    # 断言保存账号、registration success 结果和 password_setup_handoff=True。
```

- [ ] **Step 2: Run the handoff tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_registration_password_handoff.py tests/test_roxy_password_setup.py -k "handoff or enqueue_failure" -q
```

预期：当前注册服务不会识别 handoff，设置密码失败只写 `failed`，也不会在注册函数返回后自动入队。

- [ ] **Step 3: Implement post-cleanup handoff**

修改 `core/roxy_registration.py`：

1. 设置密码异常时仍保存已取得的 access token 和账号信息。
2. 返回结果增加 `password_setup_handoff=True`；`PasswordAlreadySetError` 和即时成功返回
   `False`。
3. 账号扩展字段保留 `password_setup_status=failed` 作为入队前的可追踪状态，避免账号
   保存和后台任务之间出现无状态窗口。

修改 `core/registration_service.py`：

1. `run_registration(...)` 返回后先判断成功并完成现有 job 更新。
2. 仅当 `success=True`、`password_setup_handoff=True`、存在 `account_id` 时，调用
   `enqueue_account_password_setup(account_id=int(account_id), mode="", password="", trigger="registration_handoff")`。
3. 入队调用必须位于 `run_registration` 返回之后，因此 Roxy driver/profile 的 `finally`
   清理已经完成；日志记录 `password_setup=queued` 或 `password_setup=queue_failed`。
4. 入队异常只更新账号设置密码状态，不改变 job 的成功状态。

修改 `core/db.py`：

1. 增加或复用 `password_setup_trigger`、`password_setup_last_error` 字段记录 handoff 来源。
2. 允许 handoff 账号从 `failed` 进入 `queued`，但仍拒绝 `queued/running` 重复领取。
3. 不在 handoff 阶段写入目标密码；只有 `update_account_password_setup(..., {"ok": True, "password": ...})`
   才写 `registration_password`。

- [ ] **Step 4: Run handoff and registration regression tests**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_registration_password_handoff.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py -q
```

预期：注册成功与设置密码失败彻底解耦；profile 清理顺序测试通过。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add core/roxy_registration.py core/registration_service.py core/db.py tests/test_registration_password_handoff.py tests/test_roxy_password_setup.py
git commit -m "feat: 注册成功后自动交接设置密码"
```

检查点：模拟注册失败后的清理顺序，确认后台任务不会与临时 Roxy profile 并发操作。

### Task 4: 后台退避重试与密码目标生命周期

**Files:**
- Modify: `core/password_setup_task_service.py:50-115,292-415,489-565`
- Modify: `core/db.py:1400-1518`
- Modify: `config/roxybrowser.py:68-75`
- Test: `tests/test_password_setup_task_service.py`
- Test: `tests/test_password_setup_concurrency.py`

**Interfaces:**
- Preserve `_run_password_setup_task(...) -> dict` and `_run_task_wrapper(...) -> dict`。
- `_schedule_password_setup_retry(...) -> bool` 改为延迟调度，但不占用 `_QUEUE_SLOTS`；实际提交仍调用 `_run_task_wrapper`。
- `ROXY_PASSWORD_SETUP_MAX_RETRIES=3` 表示追加重试次数；数据库中的 `password_setup_attempt` 从 1 开始记录当前执行次数，`password_setup_max_attempts` 记录总执行次数 4。

- [ ] **Step 1: Write failing retry and lifecycle tests**

在 `tests/test_password_setup_task_service.py` 增加：

```python
def test_handoff_task_generates_one_password_for_all_attempts():
    # 配置密码为空，patch _generate_password；首次执行失败并触发重试；
    # 断言同一任务的每次 runner 收到相同目标密码，成功后才写回 registration_password。

def test_retry_delays_are_15_60_180_seconds_without_holding_worker_slot():
    # patch threading.Timer、_QUEUE_SLOTS 和 db.requeue_account_password_setup；
    # 断言 delay 依次为 15、60、180，Timer 创建期间不 acquire queue slot。

def test_handoff_accepts_failed_status_but_rejects_queued_or_running_duplicate():
    # db.claim_account_password_setup(trigger="registration_handoff") 对 failed 返回 True，
    # 对 queued/running 返回 False。
```

在 `tests/test_password_setup_concurrency.py` 保留现有并发 gate 测试，并增加断言延迟重试
不会同时增加 active worker 数量。

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py -k "handoff or retry or worker" -q
```

预期：当前实现立即重新入队、把配置值当总尝试次数，且没有统一的 handoff 密码生命周期。

- [ ] **Step 3: Implement retry scheduling and DB state**

修改 `core/password_setup_task_service.py`：

1. `_max_password_setup_attempts()` 返回 `1 + configured_retries`，其中 configured_retries
   来自 `ROXY_PASSWORD_SETUP_MAX_RETRIES`，边界仍为 1–10。
2. 在 `_run_password_setup_task` 开始时解析一次目标密码；将其通过同一 `_run_task_wrapper`
   调用链传递给本任务的所有重试，不在每次重试重新生成。
3. `_schedule_password_setup_retry` 使用 `threading.Timer(delay, callback)`，delay 由
   `attempt` 映射 `{1: 15, 2: 60, 3: 180}`；Timer 设为 daemon，不占 `_QUEUE_SLOTS`。
4. Timer 到期后再 acquire slot 并提交 `_run_task_wrapper`；提交失败时回写 failed 和错误。
5. `_append_password_setup_log` 记录 attempt、max_attempts、delay 和 queue_tail，不打印密码。
6. `_open_profile_with_recovery` 保持现有失效 profile 自动创建新环境逻辑；每次后台执行都
   重新进入 `_run_roxy_password_setup`，从而获得全新的 OTP baseline。

修改 `core/db.py`：

1. `claim_account_password_setup` 对 `trigger="registration_handoff"` 允许 `failed` 行重新
   进入 queued，其余 queued/running 继续拒绝重复领取。
2. 增加 `password_setup_next_retry_at` 的清理、写入和展示字段；即时入队时置空，延迟
   retry 时写入下次时间。
3. `update_account_password_setup` 成功后保存密码，失败只写错误和状态；不把失败任务的
   临时密码写入账号。
4. `recover_interrupted_password_setups` 继续把 queued/running 标记 failed，并保留手动
   重新入队能力。

- [ ] **Step 4: Run the backend queue tests**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py -q
```

预期：队列并发、失效 profile 恢复、成功密码落库、重试状态和服务重启恢复全部通过。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add core/password_setup_task_service.py core/db.py config/roxybrowser.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py
git commit -m "feat: 设置密码失败后按退避策略后台重试"
```

检查点：确认延迟 Timer 不会让 worker 数量超过配置值，且重试不会泄漏密码。

### Task 5: 状态展示、集成回归与运行态验收

**Files:**
- Modify: `webui/app.py:135-205,778-790,2420-2455`（状态字段、队列状态和文案）
- Modify: `webui/templates/index.html`（账号列表中的设置密码状态标签和重试入口）
- Modify: `tests/test_webui_account_features.py`
- Modify: `tests/test_webui_jobs.py`
- Create: `tests/test_post_registration_password_setup_integration.py`
- Modify: `docs/superpowers/specs/2026-08-17-post-registration-password-setup-otp-design.md`（若实施细节与验证结果需补充）

**Interfaces:**
- API 继续返回 `password_setup_status`、`password_setup_attempt`、`password_setup_max_attempts`、`password_setup_next_retry_at`。
- UI 文案固定映射：`queued=注册成功，等待设置密码`、`running=正在设置密码`、
  `success=密码设置成功`、`already_set=密码已存在`、`failed=设置密码失败，可重试`。
- 手动重试继续调用 `password_setup_task_service.enqueue_account_password_setup`，不增加第二套队列。

- [ ] **Step 1: Write failing UI/integration tests**

在 `tests/test_webui_account_features.py` 增加状态映射断言：

```python
def test_account_payload_exposes_password_setup_retry_fields():
    # queued/running/failed 账号响应包含 password_setup_status、attempt、max_attempts、next_retry_at

def test_password_setup_status_labels_distinguish_registration_success():
    # registration success + password_setup_status=queued 的页面文案不显示“注册失败”
```

创建 `tests/test_post_registration_password_setup_integration.py`，使用 fake runner、fake
email provider 和 fake Roxy client 验证：

```python
def test_late_same_code_email_completes_inline_password_setup():
    # 注册码与设密码新邮件代码相同但 uid 不同；即时流程完成并保存 success

def test_inline_timeout_handoffs_after_profile_cleanup_then_background_succeeds():
    # run_registration 返回 handoff；cleanup 事件先发生；enqueue 随后执行；后台新环境登录并成功
```

- [ ] **Step 2: Run the focused UI/integration tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py tests/test_webui_jobs.py tests/test_post_registration_password_setup_integration.py -q
```

预期：当前 UI 没有 next retry 字段，且注册成功/设密 queued 的文案无法区分。

- [ ] **Step 3: Implement status presentation and integration assertions**

1. 在 `webui/app.py` 的账号 payload 和列表字段中加入 `password_setup_attempt`、
   `password_setup_max_attempts`、`password_setup_next_retry_at`。
2. 在 `webui/templates/index.html` 复用现有状态组件，确保设置密码状态独立于注册任务状态；
   queued 状态显示预计重试时间，failed 状态保留“重新设置密码”入口。
3. 不在前端渲染目标密码或 OTP；继续使用现有后端复制接口的权限和脱敏规则。
4. 集成测试确认 Roxy profile cleanup 事件先于 handoff enqueue，且 registration job 保持 success。

- [ ] **Step 4: Run complete verification**

依次运行：

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_webui_account_features.py tests/test_webui_jobs.py tests/test_post_registration_password_setup_integration.py -q
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
```

预期：聚焦测试和全量测试均为 0 failures；`git diff --check` 无输出。

随后重启项目，检查：

1. WebUI 监听 `127.0.0.1:5001`；
2. 启动日志无 ImportError；
3. 账号接口返回 200；
4. 一次实际 GenericAPI 注册中未在设密验证码页立即重复 Resend；
5. 设密晚到时能完成 settle，或注册成功后进入 queued 并由后台接管。

- [ ] **Step 5: Commit the integration and UI change**

```powershell
git add webui/app.py webui/templates/index.html tests/test_webui_account_features.py tests/test_webui_jobs.py tests/test_post_registration_password_setup_integration.py
git commit -m "test: 验证注册后设密后台续跑全链路"
```

检查点：提交前只包含本任务文件；保留工作区其他用户修改，不执行强制 reset、批量删除或远程推送。

## Final Handoff

实现全部任务后，按 `superpowers:verification-before-completion` 执行最后验证，并报告：

- 每个检查点的提交哈希和聚焦测试结果；
- 全量 pytest 总数和失败数；
- 实际重启后的 WebUI 进程/端口状态；
- 一次真实任务中 OTP 挑战、后台 handoff 和最终 `password_setup_status`；
- 未修改的用户工作区文件清单。
