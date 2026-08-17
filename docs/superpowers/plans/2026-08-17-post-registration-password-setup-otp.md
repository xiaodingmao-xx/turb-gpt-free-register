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
- `ROXY_PASSWORD_SETUP_MAX_RETRIES=3` 保持现有语义：表示后台最大总尝试次数 3；默认退避为 15 秒、60 秒，配置为 4 次以上时才使用 180 秒档。
- 注册成功与设置密码状态分离；设置密码失败不得把注册任务改成失败或释放已确认邮箱。
- 后台入队必须发生在 `run_roxy_registration` 的 driver/profile 清理完成之后。
- 不打印目标密码；新增 OTP 日志使用消息身份、时间和脱敏状态，不新增明文验证码日志。
- 每个任务结束时运行对应的聚焦 pytest；所有任务完成后运行全量 pytest。
- 保留工作区内与本功能无关的未提交修改，不执行批量删除或覆盖。
- 当前工作区的 `core/generic_api_mail_client.py`、`core/roxy_registration.py`、`core/db.py`、`core/registration_service.py`、WebUI 和对应测试已有用户未提交修改；实施时只能用 `apply_patch` 做小范围编辑，并用 `git add -p` 只暂存本任务新增 hunk，禁止整文件 `git add`。

---

### Task 0: 建立脏工作区基线与保护边界

**Files:**
- Read only: `git status` 中所有已修改和未跟踪文件
- Read only: 本计划各任务列出的生产代码与测试文件

**Interfaces:**
- Produces: 每个重叠文件的执行前 diff 记录、当前测试基线、每批允许修改的精确函数范围。
- Consumes: 当前工作区用户修改；不得提交、还原或覆盖这些既有 hunk。

- [ ] **Step 1: Record the dirty-file baseline**

运行并保存终端输出到任务记录，不创建新的仓库文件：

```powershell
git status --short
git diff -- core/generic_api_mail_client.py core/roxy_registration.py core/db.py core/registration_service.py core/password_setup_task_service.py webui/app.py webui/templates/index.html
git diff -- tests/test_generic_api_yangyang.py tests/test_roxy_password_setup.py tests/test_password_setup_task_service.py tests/test_webui_account_features.py tests/test_webui_jobs.py
```

预期：这些文件中存在本计划开始前的用户修改。后续每批只编辑计划点名的函数或测试方法。

- [ ] **Step 2: Run the pre-implementation test baseline**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py tests/test_roxy_password_setup.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_webui_account_features.py tests/test_webui_jobs.py -q
```

预期：记录准确的通过数和任何既有失败。若存在失败，先判断是否与本功能相关；不得把既有
失败混入本功能的 GREEN 结论。

- [ ] **Step 3: Define the checkpoint staging rule**

每批提交前必须执行：

```powershell
git diff --cached --check
git diff --cached --stat
```

先使用该任务 Step 5 列出的精确路径执行 `git diff --` 和 `git add -p --`；只有本批新增
hunk 可以进入暂存区。若一个 hunk 同时包含用户旧改动和本批改动，先用
`apply_patch` 把本批改动拆成独立 hunk；无法拆分时不提交该文件，只保留测试检查点并向
用户说明。

检查点：Task 0 不创建提交。

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

在 `tests/test_generic_api_yangyang.py` 增加可控会话和时钟：

同时从 `core.generic_api_mail_client` 导入 `_matches_otp_baseline`；实现步骤完成后再导入并
直接测试 `_otp_observation_key`。

```python
class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.001)


class FakeSequenceSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def get(self, _url, **_kwargs):
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return FakeResponse(text=json.dumps(self.payloads[index]))
```

然后增加四个具体测试：

```python
def test_same_code_from_new_message_id_is_accepted(self):
    baseline = OtpBaseline(frozenset({"119006"}), frozenset({"mail-old"}), 999.0)
    observation = _parse_generic_api_observation(json.dumps({
        "found": True,
        "ok": True,
        "message": {"code": "119006", "timestamp": 1001.0, "uid": "mail-new"},
    }), after_ts=1000.0, now_ts=1001.0)
    self.assertEqual(observation.code, "119006")
    self.assertFalse(_matches_otp_baseline(observation, baseline, 1000.0))

def test_candidate_seen_before_search_deadline_gets_full_settle_window(self):
    clock = FakeClock()
    payloads = [{"found": False, "ok": True}] * 9 + [{
        "found": True,
        "ok": True,
        "message": {"code": "119006", "timestamp": 1009.0, "uid": "mail-new"},
    }]
    session = FakeSequenceSession(payloads)
    # patch account、Session、time.time、time.sleep；max_wait=10、poll_interval=1、settle=5
    # 断言返回 "119006" 且 clock.now >= 1014.0。

def test_same_code_new_message_resets_settle(self):
    clock = FakeClock()
    payloads = [
        {"message": {"code": "119006", "timestamp": 1000.0, "uid": "mail-1"}},
        {"message": {"code": "119006", "timestamp": 1000.0, "uid": "mail-1"}},
        {"message": {"code": "119006", "timestamp": 1002.0, "uid": "mail-2"}},
    ]
    # settle=3；断言同码 mail-2 使返回时间从 1003 延后到至少 1005。

def test_unstable_candidate_fails_after_confirmation_hard_limit(self):
    clock = FakeClock()
    payloads = [
        {"message": {"code": "119006", "timestamp": 1000.0 + i, "uid": f"mail-{i}"}}
        for i in range(30)
    ]
    # settle=2；每轮候选身份都变化。断言 GenericApiMailError 包含“候选不稳定”，
    # 且 clock.now >= 1015.0。
```

第一个测试是现有行为的 characterization，必须先通过；后三个测试用于 RED。

- [ ] **Step 2: Run the focused tests and verify RED**

运行：

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py -k "same_code or settle or unstable" -q
```

预期：`test_same_code_from_new_message_id_is_accepted` 通过；后三个新增测试失败。现有实现
会按验证码字符串比较，并在总 deadline 到达时直接抛出 `settle 未完成`。

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

4. 新候选判定继续调用 `_matches_otp_baseline`。保留 `exclude_codes` 的现有含义：只有调用方
   明确传入、表示页面已经拒绝过该码时才按值排除；Task 2 的设置密码流程不会传该参数。
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
git add -p -- core/generic_api_mail_client.py config/email.py tests/test_generic_api_yangyang.py tests/test_config_defaults.py
git commit -m "fix: 分离OTP搜索与settle确认窗口"
```

检查点：提交后记录测试结果，确认未暂存其他工作区文件。

### Task 2: 设置密码独立挑战与取消首次立即重发

**Files:**
- Modify: `core/roxy_registration.py:1749-1845`（`_run_roxy_password_setup`）
- Test: `tests/test_roxy_password_setup.py`
- Test: `tests/test_roxy_registration_otp_retry.py`

**Interfaces:**
- Preserve `_run_roxy_password_setup(driver, email, mode=None, password=None, previous_otp=None, progress_callback=None) -> str`。
- Each OTP wait continues to call `wait_for_otp(email, after_ts=otp_after_ts, otp_baseline=otp_baseline)`；设置密码流程不再传 `exclude_codes`，因为跨业务阶段不能仅凭验证码值判旧。
- Use existing `capture_otp_baseline(email)` and `_click_resend_email_otp(driver, timeout=25)`.

- [ ] **Step 1: Write failing tests for the password setup challenge order**

先更新两个与旧行为绑定的现有测试：

- 把 `test_password_setup_resends_and_excludes_previous_registration_otp` 改为
  `test_password_setup_initial_attempt_does_not_resend_or_exclude_previous_code`；
- 把 `test_password_setup_allows_generic_api_to_reuse_previous_otp_after_resend` 改为
  `test_password_setup_allows_generic_api_to_reuse_previous_otp_without_resend`；
- 两个测试都 patch `capture_otp_baseline`，断言首次 `resend.assert_not_called()`，并断言
  `wait_for_otp.call_args.kwargs` 不含 `exclude_codes`。

再增加按调用顺序记录事件的测试：

```python
def test_password_setup_resend_refreshes_baseline_before_trigger():
    events = []
    baselines = [object(), object()]
    with patch("core.roxy_registration.capture_otp_baseline", side_effect=lambda _email: (
        events.append("baseline") or baselines.pop(0)
    )), patch("core.roxy_registration._click_resend_email_otp", side_effect=lambda *_a, **_k: (
        events.append("resend")
    )), patch("core.roxy_registration.wait_for_otp", side_effect=[
        GenericApiMailError("timeout"), "119006"
    ]):
        # 同时 patch authorize、安全导航、页面状态、OTP 输入/提交和密码页。
        result = _run_roxy_password_setup(driver, "user@example.com", password="valid-password-123")
    self.assertEqual(result, "valid-password-123")
    self.assertEqual(events[:3], ["baseline", "baseline", "resend"])
```

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_password_setup.py -k "initial_password_setup or same_code or refreshes_baseline" -q
```

预期：现有代码会在首次进入验证码页后立即调用 Resend，且 GenericAPI 的首次等待不带
独立 baseline。

- [ ] **Step 3: Implement per-attempt challenge lifecycle**

修改 `_run_roxy_password_setup`：

1. 在 `_fetch_password_setup_authorize_url` 之前调用 `capture_otp_baseline(email)`；完成基线
   抓取后、authorize 请求前记录 `otp_after_ts = time.time()`。非 GenericAPI 来源返回
   `None` baseline。
2. 打开 authorize URL 并确认进入邮箱验证码页后，直接执行第一次 `wait_for_otp`；删除当前
   “只要有 previous_otp 就先 Resend”的无条件分支。
3. 设置密码流程对所有 provider 都不传 `exclude_codes`：Outlook/GPTMail 等继续用
   `after_ts` 判断新鲜度，GenericAPI 额外使用 baseline/消息 ID/时间戳。
4. 用 `try/except` 捕获 `wait_for_otp` 的超时；有剩余轮次时不要退出整个设密流程，而是
   和页面明确拒绝走同一个重发分支。下一轮按此顺序执行：

```python
otp_baseline = capture_otp_baseline(email)
otp_after_ts = time.time()
_click_resend_email_otp(driver, timeout=25)
code = wait_for_otp(email, after_ts=otp_after_ts, otp_baseline=otp_baseline)
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
git add -p -- core/roxy_registration.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py
git commit -m "fix: 隔离注册与设密验证码挑战"
```

检查点：确认首次设密挑战不再自动重复发送邮件。

### Task 3: 注册成功后的设置密码 handoff

**Files:**
- Modify: `core/roxy_registration.py:2427-2534`（注册结果、账号保存和清理边界）
- Modify: `core/registration_service.py:421-500`（注册返回后的后台入队）
- Create: `tests/test_registration_password_handoff.py`
- Modify: `tests/test_roxy_password_setup.py`

**Interfaces:**
- `run_roxy_registration` 成功返回增加内部字段 `password_setup_handoff: bool`；该字段不进入普通账号密钥输出。
- `password_setup_task_service.enqueue_account_password_setup(*, account_id: int, mode: str, password: str, trigger: str = "manual") -> dict` 是唯一入队入口。
- Add `registration_service._enqueue_password_setup_handoff(result: dict) -> dict | None`；无 handoff 时返回 `None`，有 handoff 时返回队列结果。

- [ ] **Step 1: Write failing handoff tests**

创建 `tests/test_registration_password_handoff.py`，覆盖以下可执行断言：

```python
def test_registration_success_with_password_handoff_queues_after_runner_returns():
    events = []
    result = {
        "success": True, "account_id": 42, "password_setup_handoff": True,
        "email": "user@example.com",
    }
    # fake run_registration 在返回前 append "runner_returning"；fake enqueue append "enqueue"。
    # 运行 _run_one_job 后断言 events == ["runner_returning", "enqueue"]，
    # 且 db.update_job 最终收到 status="success"。

def test_password_handoff_enqueue_failure_keeps_registration_success():
    result = {"success": True, "account_id": 42, "password_setup_handoff": True}
    # enqueue 返回 {"accepted": False, "error": "queue full"}；
    # 断言 db.update_account_password_setup(42, {"ok": False, "error": "queue full"}) 被调用，
    # 但 db.update_job 没有收到 status="failed"。
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

1. 增加 `_enqueue_password_setup_handoff(result)`，仅当 `success=True`、
   `password_setup_handoff=True`、存在 `account_id` 时调用
   `enqueue_account_password_setup(account_id=int(account_id), mode="", password="", trigger="registration_handoff")`。
2. 队列返回 `accepted=False` 或抛出异常时，调用
   `db.update_account_password_setup(account_id, {"ok": False, "error": error_text})`，并返回失败
   摘要；不得修改 registration job 状态。
3. `_run_one_job` 在把成功 registration job 写为 success 后调用该 helper。入队调用位于
   `run_registration` 返回之后，因此 Roxy driver/profile 的 `finally`
   清理已经完成；日志记录 `password_setup=queued` 或 `password_setup=queue_failed`。
4. 入队异常只更新账号设置密码状态，不改变 job 的成功状态。

现有 `db.claim_account_password_setup` 已经只拒绝 `queued/running`，能从 inline failure 的
空状态或 failed 状态进入 queued；本任务不修改该逻辑。现有成功落库逻辑也已经只在
`ok=True` 时保存 `registration_password`。

- [ ] **Step 4: Run handoff and registration regression tests**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_registration_password_handoff.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py -q
```

预期：注册成功与设置密码失败彻底解耦；profile 清理顺序测试通过。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add -p -- core/roxy_registration.py core/registration_service.py tests/test_registration_password_handoff.py tests/test_roxy_password_setup.py
git commit -m "feat: 注册成功后自动交接设置密码"
```

检查点：模拟注册失败后的清理顺序，确认后台任务不会与临时 Roxy profile 并发操作。

### Task 4: 后台退避重试与密码目标生命周期

**Files:**
- Modify: `core/password_setup_task_service.py:50-115,292-415,489-565`
- Modify: `core/db.py:1400-1518`
- Test: `tests/test_password_setup_task_service.py`
- Test: `tests/test_password_setup_concurrency.py`

**Interfaces:**
- Preserve `_run_password_setup_task(*, account_id: int, email: str, mode: str, password: str) -> dict` and `_run_task_wrapper(*, account_id: int, email: str, mode: str, password: str) -> dict`。
- Add `_retry_delay_seconds(attempt: int) -> int`，按当前失败 attempt 返回下一次执行前的延迟：1→15、2→60、3 及以上→180。
- `_schedule_password_setup_retry(*, account_id: int, email: str, mode: str, password: str, result: dict) -> bool` 改为延迟调度，但不占用 `_QUEUE_SLOTS`；实际提交仍调用 `_run_task_wrapper`。
- `ROXY_PASSWORD_SETUP_MAX_RETRIES=3` 保持现有“最大总尝试次数”语义；`password_setup_attempt` 从 1 开始，默认最多执行 3 次。
- Change `db.requeue_account_password_setup(acc_id: int, error: str, *, attempt: int, max_attempts: int, next_retry_at: str | None = None) -> bool` to persist delayed state.

- [ ] **Step 1: Write failing retry and lifecycle tests**

在 `tests/test_password_setup_task_service.py` 增加：

```python
def test_handoff_task_generates_one_password_for_all_attempts():
    submitted_passwords = []
    class FakeExecutor:
        def submit(self, _fn, **kwargs):
            submitted_passwords.append(kwargs["password"])
            return object()
    with patch.object(service, "resolve_password_setup_request", return_value=(
        "post_login_add_password", "generated-once"
    )) as resolve, patch.object(service, "_EXECUTOR", FakeExecutor()):
        # 同时 patch db.get_account、db.claim_account_password_setup 和 queue slot。
        service.enqueue_account_password_setup(
            account_id=7, mode="", password="", trigger="registration_handoff"
        )
    resolve.assert_called_once()
    self.assertEqual(submitted_passwords, ["generated-once"])
    failed_result = {
        "ok": False, "retryable": True, "attempt": 1,
        "max_attempts": 3, "error": "timeout",
    }
    # 调用 _schedule_password_setup_retry(account_id=7, email="user@example.com",
    # mode="post_login_add_password", password="generated-once", result=failed_result)
    # 并执行 fake Timer
    # callback 后，再断言 submitted_passwords == ["generated-once", "generated-once"]。

def test_retry_delays_are_15_60_180_seconds_without_holding_worker_slot():
    self.assertEqual([service._retry_delay_seconds(i) for i in (1, 2, 3, 4)], [15, 60, 180, 180])
    # patch threading.Timer、_QUEUE_SLOTS 和 db.requeue_account_password_setup；
    # 调用 _schedule_password_setup_retry 后断言 Timer(delay, callback) 被创建并 start，
    # 但 _QUEUE_SLOTS.acquire 尚未调用；手动执行 callback 后才 acquire。

def test_default_retry_limit_remains_three_total_attempts():
    with patch.object(roxy_cfg, "ROXY_PASSWORD_SETUP_MAX_RETRIES", 3):
        self.assertEqual(service._max_password_setup_attempts(), 3)
```

在 `tests/test_password_setup_concurrency.py` 保留现有并发 gate 测试，并增加断言延迟重试
不会同时增加 active worker 数量。

- [ ] **Step 2: Run the focused tests and verify RED**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py -k "handoff or retry or worker" -q
```

预期：密码只生成一次和默认总尝试次数的 characterization 通过；延迟调度测试失败，因为
当前实现会立即占用队列槽并重新提交。

- [ ] **Step 3: Implement retry scheduling and DB state**

修改 `core/password_setup_task_service.py`：

1. 保留 `_max_password_setup_attempts()` 当前语义和默认返回值 3，不修改
   `config/roxybrowser.py`。
2. 保留 `enqueue_account_password_setup` 在入队时调用一次
   `resolve_password_setup_request(mode, password)` 的现有行为；Timer 和所有重试继续传递
   同一个 `password` 参数，不在 runner 内重新生成。
3. 添加 `_retry_delay_seconds`，使用延迟序列 `(15, 60, 180)`，超过序列后固定返回 180。
4. `_schedule_password_setup_retry` 先调用 `db.requeue_account_password_setup(account_id,
   error, attempt=next_attempt, max_attempts=max_attempts, next_retry_at=retry_at)`，再创建
   `threading.Timer(delay, callback)`；Timer 设为 daemon，启动
   Timer 时不得 acquire `_QUEUE_SLOTS`。
5. Timer callback 使用 `_QUEUE_SLOTS.acquire(blocking=False)`。队列已满时不消耗 attempt，
   把 `next_retry_at` 顺延 5 秒并重新创建 Timer；获得槽位后清空 `next_retry_at`，再提交
   `_run_task_wrapper`。executor 提交失败时释放槽位并写回 failed。
6. `_append_password_setup_log` 记录 attempt、max_attempts、delay 和 next_retry_at，不打印密码。
7. `_open_profile_with_recovery` 保持现有失效 profile 自动创建新环境逻辑；每次后台执行都
   重新进入 `_run_roxy_password_setup`，从而获得全新的 OTP baseline。

修改 `core/db.py`：

1. `claim_account_password_setup` 保持当前去重规则，只拒绝 queued/running；即时入队时
   把 `password_setup_next_retry_at` 置空。
2. `requeue_account_password_setup` 增加 `next_retry_at` 参数并写入
   `password_setup_next_retry_at`；延迟
   retry 时写入下次时间。
3. `update_account_password_setup` 成功后保存密码，失败只写错误和状态；不把失败任务的
   临时密码写入账号，同时清空 `password_setup_next_retry_at`。
4. `mark_account_password_setup_running` 清空 `password_setup_next_retry_at`；
   `recover_interrupted_password_setups` 继续把 queued/running 标记 failed、清空该字段，并保留手动
   重新入队能力。

修改 `queue_settings()`：未来时间的 `password_setup_next_retry_at` 计入 `delayed`，不分配
执行器队列位置；普通 queued 继续计入 `waiting` 和 `positions`。

- [ ] **Step 4: Run the backend queue tests**

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py -q
```

预期：队列并发、失效 profile 恢复、成功密码落库、重试状态和服务重启恢复全部通过。

- [ ] **Step 5: Commit the independently testable change**

```powershell
git add -p -- core/password_setup_task_service.py core/db.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_account_list_query.py
git commit -m "feat: 设置密码失败后按退避策略后台重试"
```

检查点：确认延迟 Timer 不会让 worker 数量超过配置值，且重试不会泄漏密码。

### Task 5: 状态展示、集成回归与运行态验收

**Files:**
- Modify: `webui/app.py:141-203`（仅补充延迟重试字段和队列摘要）
- Modify: `webui/templates/index.html:3475-3505`（仅在现有密码状态单元格显示下次重试时间）
- Modify: `tests/test_webui_account_features.py`
- Create: `tests/test_post_registration_password_setup_integration.py`

**Interfaces:**
- API 继续返回 `password_setup_status`、`password_setup_attempt`、`password_setup_max_attempts`、`password_setup_next_retry_at`。
- UI 文案固定映射：`queued=注册成功，等待设置密码`、`running=正在设置密码`、
  `success=密码设置成功`、`already_set=密码已存在`、`failed=设置密码失败，可重试`。
- `queue_settings()` 额外返回 `delayed`；延迟中的 queued 账号不显示执行器队列位置。
- 手动重试继续调用 `password_setup_task_service.enqueue_account_password_setup`，不增加第二套队列。

- [ ] **Step 1: Write failing UI/integration tests**

在 `tests/test_webui_account_features.py` 增加状态映射断言：

```python
@patch("webui.app.password_setup_task_service.queue_settings")
@patch("webui.app.db.list_accounts_page")
def test_account_payload_exposes_password_setup_retry_fields(self, list_page, queue_settings):
    list_page.return_value = {"items": [{
        "id": 7,
        "password_setup_status": "queued",
        "password_setup_attempt": 2,
        "password_setup_max_attempts": 3,
        "password_setup_next_retry_at": "2026-08-17T16:30:00",
    }], "total": 1, "sources": [], "revision": "1:now"}
    queue_settings.return_value = {"positions": {}, "waiting": 0, "delayed": 1}
    response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")
    row = response.get_json()["items"][0]
    self.assertEqual(row["password_setup_next_retry_at"], "2026-08-17T16:30:00")
    self.assertEqual(response.get_json()["password_setup_queue"]["delayed"], 1)

def test_password_setup_status_labels_distinguish_registration_success(self):
    template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    self.assertIn("注册成功，等待设置密码", html)
    self.assertIn("password_setup_next_retry_at", html)
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
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py tests/test_post_registration_password_setup_integration.py -q
```

预期：现有 UI 已经区分 queued/running/failed，因此对应 characterization 通过；
`password_setup_next_retry_at` 和 delayed 摘要测试失败。

- [ ] **Step 3: Implement status presentation and integration assertions**

1. `webui/app.py` 已经返回 attempt/max_attempts/last_error，只需在
   `_compact_account_for_list` 增加 `password_setup_next_retry_at`，并原样返回
   `queue_settings()` 的 delayed 摘要。
2. 在 `webui/templates/index.html` 现有 `_registrationPasswordCell` 中：queued 且
   `password_setup_next_retry_at` 有值时显示“注册成功，等待设置密码 · HH:MM:SS 后重试”；
   普通 queued 继续显示队列位置，running/success/already_set/failed 现有分支不重写。
3. 不在前端渲染目标密码或 OTP；继续使用现有后端复制接口的权限和脱敏规则。
4. 集成测试确认 Roxy profile cleanup 事件先于 handoff enqueue，且 registration job 保持 success。

- [ ] **Step 4: Run complete verification**

依次运行：

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_generic_api_yangyang.py tests/test_roxy_password_setup.py tests/test_roxy_registration_otp_retry.py tests/test_registration_password_handoff.py tests/test_password_setup_task_service.py tests/test_password_setup_concurrency.py tests/test_webui_account_features.py tests/test_webui_jobs.py tests/test_post_registration_password_setup_integration.py -q
& '.venv\Scripts\python.exe' -m pytest -q
git diff --check
```

预期：聚焦测试和全量测试均为 0 failures；`git diff --check` 无输出。

随后重启项目，检查：

1. WebUI 监听 `127.0.0.1:5001`；
2. 启动日志无 ImportError；
3. 账号接口返回 200；
4. fake provider 集成测试证明设密验证码页不会立即重复 Resend；
5. fake 慢邮件证明能完成 settle，或注册成功后进入 queued 并由后台接管。

真实 GenericAPI 注册会消耗邮箱、代理和外部账号资源，不属于自动验收。只有用户再次明确
批准运行真实注册任务后，才执行一条真实任务作为补充验证。

- [ ] **Step 5: Commit the integration and UI change**

```powershell
git add -p -- webui/app.py webui/templates/index.html tests/test_webui_account_features.py tests/test_post_registration_password_setup_integration.py
git commit -m "test: 验证注册后设密后台续跑全链路"
```

检查点：提交前只包含本任务文件；保留工作区其他用户修改，不执行强制 reset、批量删除或远程推送。

## Final Handoff

实现全部任务后，按 `superpowers:verification-before-completion` 执行最后验证，并报告：

- 每个检查点的提交哈希和聚焦测试结果；
- 全量 pytest 总数和失败数；
- 实际重启后的 WebUI 进程/端口状态；
- fake 集成任务中的 OTP 挑战、后台 handoff 和最终 `password_setup_status`；如用户另行批准
  真实注册，再补充真实任务证据；
- 未修改的用户工作区文件清单。
