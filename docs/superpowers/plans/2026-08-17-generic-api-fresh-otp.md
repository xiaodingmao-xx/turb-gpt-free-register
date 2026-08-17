# GenericAPI 新验证码判定实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 阻止注册流程把 GenericAPI 取码接口缓存的旧验证码当作本次新验证码提交，并在邮件时间、接口基线和失败排除三个层面建立可验证的新鲜度判断。

**Architecture:** 在触发 OpenAI 发信前，对 GenericAPI 取码地址执行一次非阻塞基线快照；轮询时只接受晚于触发时间的邮件，或者在接口不提供时间戳时接受与基线及已拒绝集合不同的验证码。结构化 JSON 的解析结果要携带“已识别但因过期被拒绝”的状态，避免旧验证码被通用正文正则再次提取；候选只有通过新鲜度检查后才能进入 settle，超时也不返回未确认候选。

**Tech Stack:** Python 3、requests、dataclasses、unittest/pytest、现有 `core.email_provider` 邮箱来源路由。

## Global Constraints

- 默认最大邮件年龄为 `3600` 秒；它是额外保险，不能代替 `msg_ts >= after_ts - 2` 的本次触发时间校验。
- GenericAPI 响应没有时间戳时，必须依靠触发前基线及 `exclude_codes` 判定；基线请求失败时默认终止本次注册，不能降级为盲目提交接口当前值。
- `settle` 只用于观察已经通过新鲜度校验的候选是否变化，不能作为新邮件判据。
- 结构化 JSON 已被识别后，无论是过期、字段非法还是没有验证码，都不得回退到全文正则扫描同一份 JSON。
- Outlook、GPTMail、MailNest、CloudMail、Cloudflare 等非 GenericAPI 邮箱来源保持现有调用和行为。
- 不记录邮箱 API 密钥、完整响应正文或访问令牌；诊断日志只记录来源、时间戳、message id、拒绝原因和已有验证码日志格式。
- 全部修改采用 TDD；每个任务先写失败测试，再写最小实现。

---

## 文件结构

- Modify: `config/email.py` — 新增 GenericAPI 新鲜度配置及环境变量覆盖。
- Modify: `config/__init__.py` — 导出新增配置。
- Modify: `webui/config_editor.py` — 在邮箱配置区暴露最大邮件年龄和严格基线开关。
- Modify: `core/generic_api_mail_client.py` — 统一解析响应、执行时间/年龄过滤、抓取基线、排除旧候选并修复超时返回。
- Modify: `core/email_provider.py` — 为注册层提供按邮箱来源路由的基线快照接口，并把基线传给 GenericAPI 轮询器。
- Modify: `core/roxy_registration.py` — 在触发验证码前抓取基线并记录准确触发时间；重发后继续排除旧码。
- Modify: `tests/test_generic_api_yangyang.py` — 覆盖时间过滤、缺失时间、基线、JSON 回退和超时语义。
- Modify: `tests/test_email_provider_gptmail.py` — 覆盖基线路由只作用于 GenericAPI。
- Modify: `tests/test_roxy_registration_otp_retry.py` — 覆盖注册首轮基线和重发后的排除集合。
- Modify: `tests/test_config_defaults.py` — 固定新增配置默认值。
- Modify: `docs/superpowers/specs/2026-08-17-generic-api-otp-extraction-design.md` — 更新原设计中“无时间字段时依靠 settle”的不足。

---

### Task 1: 建立可区分“非结构化响应”和“结构化但被拒绝”的解析结果

**Files:**
- Modify: `core/generic_api_mail_client.py:250`
- Test: `tests/test_generic_api_yangyang.py`

**Interfaces:**
- Produces: `GenericOtpObservation(code, source, received_at, msg_ts, message_id, structured, rejection_reason)`。
- Produces: `_parse_generic_api_observation(text: str, after_ts: float | None = None, max_age_seconds: int | None = None, now_ts: float | None = None) -> GenericOtpObservation`。
- Preserves: `_extract_structured_api_code(text, after_ts=None) -> tuple[str, dict] | None`，作为兼容包装层供现有测试和调用使用。

- [ ] **Step 1: 写出 JSON 时间过滤和回退绕过的失败测试**

```python
def test_structured_old_code_is_recognized_but_rejected():
    payload = json.dumps({
        "code": "174510",
        "time": "2026-08-17T14:40:00+08:00",
        "message_id": "mail-old",
    })
    after = datetime.datetime.fromisoformat("2026-08-17T14:53:40+08:00").timestamp()

    observation = _parse_generic_api_observation(payload, after_ts=after)

    assert observation.structured is True
    assert observation.code is None
    assert observation.rejection_reason == "before_trigger"
    assert observation.message_id == "mail-old"


def test_structured_old_code_must_not_fall_back_to_raw_regex():
    payload = json.dumps({
        "code": "174510",
        "time": "2026-08-17T14:40:00+08:00",
    })
    after = datetime.datetime.fromisoformat("2026-08-17T14:53:40+08:00").timestamp()

    observation = _parse_generic_api_observation(payload, after_ts=after)

    assert observation.structured is True
    assert observation.code is None
```

- [ ] **Step 2: 写出最大年龄和无时间戳状态的失败测试**

```python
def test_structured_code_older_than_max_age_is_rejected():
    now_ts = 2_000.0
    payload = json.dumps({"code": "174510", "timestamp": 100.0})

    observation = _parse_generic_api_observation(
        payload,
        max_age_seconds=1_000,
        now_ts=now_ts,
    )

    assert observation.code is None
    assert observation.rejection_reason == "older_than_max_age"


def test_structured_code_without_timestamp_keeps_metadata_missing_state():
    observation = _parse_generic_api_observation(json.dumps({"code": "174510"}))

    assert observation.code == "174510"
    assert observation.structured is True
    assert observation.msg_ts is None
    assert observation.rejection_reason is None
```

- [ ] **Step 3: 运行新测试并确认失败**

Run: `python -m pytest tests/test_generic_api_yangyang.py -k "recognized_but_rejected or raw_regex or max_age or metadata_missing" -q`

Expected: FAIL，因为 `_parse_generic_api_observation` 和 `GenericOtpObservation` 尚不存在。

- [ ] **Step 4: 增加解析结果类型和统一解析器**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class GenericOtpObservation:
    code: str | None
    source: str
    received_at: object | None
    msg_ts: float | None
    message_id: str | None
    structured: bool
    rejection_reason: str | None = None


def _parse_generic_api_observation(
    text: str,
    after_ts: float | None = None,
    max_age_seconds: int | None = None,
    now_ts: float | None = None,
) -> GenericOtpObservation:
    try:
        data = json.loads(text)
    except Exception:
        return GenericOtpObservation(None, "plain_text", None, None, None, False)

    if not isinstance(data, dict):
        return GenericOtpObservation(None, "structured_api", None, None, None, True, "invalid_shape")

    message_id = data.get("message_id") or data.get("messageId") or data.get("id")
    ts_raw = (
        data.get("time") or data.get("date") or data.get("received_at")
        or data.get("receivedAt") or data.get("created_at")
        or data.get("createdAt") or data.get("timestamp")
    )
    msg_ts = _parse_generic_api_ts(ts_raw)
    code, source = _extract_code_from_structured_dict(data)
    rejection_reason = None
    now_value = time.time() if now_ts is None else now_ts

    if data.get("ok") is False or data.get("found") is False:
        code, rejection_reason = None, "not_found"
    elif code and after_ts is not None and msg_ts is not None and msg_ts + 2 < after_ts:
        code, rejection_reason = None, "before_trigger"
    elif code and max_age_seconds is not None and msg_ts is not None and now_value - msg_ts > max_age_seconds:
        code, rejection_reason = None, "older_than_max_age"

    return GenericOtpObservation(
        code=code,
        source=source or "structured_api",
        received_at=ts_raw,
        msg_ts=msg_ts,
        message_id=str(message_id) if message_id is not None else None,
        structured=True,
        rejection_reason=rejection_reason,
    )
```

将当前 `_extract_structured_api_code()` 中提取 `raw_code/message` 的部分移动到 `_extract_code_from_structured_dict(data) -> tuple[str | None, str]`。兼容包装层只在 `observation.code` 非空时返回 `(code, meta)`；被拒绝时返回 `None`。

- [ ] **Step 5: 修改轮询分支，禁止结构化 JSON 回退到全文正则**

```python
observation = _parse_generic_api_observation(
    text,
    after_ts=after_ts,
    max_age_seconds=max_age_seconds,
)
if observation.structured:
    code = observation.code
else:
    code = _extract_code(text)
    observation = dataclasses.replace(
        observation,
        code=code,
        source="plain_text",
    )
```

当 `observation.rejection_reason` 非空时，将 `last_error` 设置为包含该原因、`received_at`、`message_id` 和 `after_ts` 的诊断文本，不再执行 `_extract_code(text)`。

- [ ] **Step 6: 运行解析测试和现有 GenericAPI 测试**

Run: `python -m pytest tests/test_generic_api_yangyang.py -q`

Expected: PASS，现有 CSS `#000000`、HTML 可见正文、YangYang 和排除集合测试全部保持通过。

- [ ] **Step 7: 提交解析修复**

```powershell
git add core/generic_api_mail_client.py tests/test_generic_api_yangyang.py
git commit -m "fix: 阻止旧验证码绕过结构化时间校验"
```

---

### Task 2: 增加最大邮件年龄配置并固定严格模式默认值

**Files:**
- Modify: `config/email.py:45`
- Modify: `config/email.py:151`
- Modify: `config/__init__.py:123`
- Modify: `config/__init__.py:249`
- Modify: `webui/config_editor.py:370`
- Test: `tests/test_config_defaults.py`

**Interfaces:**
- Produces: `OTP_MAX_MESSAGE_AGE_SECONDS: int = 3600`。
- Produces: `GENERIC_API_REQUIRE_BASELINE: bool = True`。

- [ ] **Step 1: 写配置默认值失败测试**

```python
def test_generic_api_otp_freshness_defaults():
    from config import email

    assert email.OTP_MAX_MESSAGE_AGE_SECONDS == 3600
    assert email.GENERIC_API_REQUIRE_BASELINE is True
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_config_defaults.py -k freshness -q`

Expected: FAIL，两个配置尚不存在。

- [ ] **Step 3: 在邮箱配置中增加默认值和环境变量覆盖**

```python
# 只在邮件响应包含可解析时间戳时生效；本次触发时间 after_ts 仍是主判据。
OTP_MAX_MESSAGE_AGE_SECONDS = 3600

# GenericAPI 缺少可靠时间戳时，要求触发前基线抓取成功。
GENERIC_API_REQUIRE_BASELINE = True
```

在 `apply_env_overrides()` 映射中加入：

```python
"OTP_MAX_MESSAGE_AGE_SECONDS": "int",
"GENERIC_API_REQUIRE_BASELINE": "bool",
```

在 `config/__init__.py` 的导入和 `__all__` 中导出这两个名称。在 `webui/config_editor.py` 邮箱区加入整数输入“OTP 最大邮件年龄（秒）”和布尔开关“GenericAPI 严格基线校验”。

- [ ] **Step 4: 将最大年龄传入 GenericAPI 解析器**

```python
max_age_seconds = max(
    0,
    int(getattr(_email_cfg, "OTP_MAX_MESSAGE_AGE_SECONDS", 3600) or 0),
)
```

`0` 表示关闭绝对年龄限制，但不关闭 `after_ts` 和基线校验。

- [ ] **Step 5: 运行配置和 WebUI 配置回归测试**

Run: `python -m pytest tests/test_config_defaults.py tests/test_webui_jobs.py -q`

Expected: PASS。

- [ ] **Step 6: 提交配置改动**

```powershell
git add config/email.py config/__init__.py webui/config_editor.py tests/test_config_defaults.py
git commit -m "feat: 增加验证码邮件新鲜度配置"
```

---

### Task 3: 实现触发前 GenericAPI 基线快照

**Files:**
- Modify: `core/generic_api_mail_client.py`
- Modify: `core/email_provider.py`
- Test: `tests/test_generic_api_yangyang.py`
- Test: `tests/test_email_provider_gptmail.py`

**Interfaces:**
- Produces: `OtpBaseline(codes: frozenset[str], message_ids: frozenset[str], captured_at: float)`。
- Produces: `core.generic_api_mail_client.capture_otp_baseline(email: str, attempts: int = 3) -> OtpBaseline`。
- Produces: `core.email_provider.capture_otp_baseline(email: str) -> OtpBaseline | None`；非 GenericAPI 返回 `None`。
- Extends: `core.email_provider.wait_for_otp(..., otp_baseline: OtpBaseline | None = None) -> str`；只向 GenericAPI 透传。
- Consumes: Task 1 的 `_parse_generic_api_observation()`。

- [ ] **Step 1: 写出基线快照失败测试**

```python
def test_capture_baseline_records_cached_code_before_trigger():
    account = GenericApiEmailAccount(
        email="user@example.com",
        code_url="https://mail.example/code",
    )
    session = FakeSingleResponseSession({
        "code": "174510",
        "message_id": "mail-before-trigger",
    })

    with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
        "core.generic_api_mail_client.requests.Session", return_value=session
    ):
        baseline = capture_otp_baseline(account.email, attempts=1)

    assert baseline.codes == frozenset({"174510"})
    assert baseline.message_ids == frozenset({"mail-before-trigger"})
    assert baseline.captured_at > 0
```

- [ ] **Step 2: 写出基线失败时禁止静默降级的测试**

```python
def test_capture_baseline_raises_after_request_failures():
    account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")

    with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
        "core.generic_api_mail_client.requests.Session.get",
        side_effect=requests.Timeout("baseline timeout"),
    ), patch("core.generic_api_mail_client.time.sleep"):
        with pytest.raises(GenericApiMailError, match="基线"):
            capture_otp_baseline(account.email, attempts=3)
```

- [ ] **Step 3: 运行基线测试并确认失败**

Run: `python -m pytest tests/test_generic_api_yangyang.py -k baseline -q`

Expected: FAIL，因为基线类型和函数尚不存在。

- [ ] **Step 4: 实现基线数据类型和最多三次的非轮询快照**

```python
@dataclass(frozen=True)
class OtpBaseline:
    codes: frozenset[str]
    message_ids: frozenset[str]
    captured_at: float


def capture_otp_baseline(email: str, attempts: int = 3) -> OtpBaseline:
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            observation = _fetch_current_observation(account)
            codes = frozenset({observation.code}) if observation.code else frozenset()
            message_ids = (
                frozenset({observation.message_id})
                if observation.message_id else frozenset()
            )
            return OtpBaseline(codes, message_ids, time.time())
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(1)

    raise GenericApiMailError(f"抓取验证码接口基线失败: {email}; {last_error}")
```

`_fetch_current_observation(account)` 复用 Task 1 解析器；YangYang 路径记录当前最新邮件的 code 和 mail id，普通 URL 发起一次 GET。该函数不执行 settle，也不等待新邮件。

- [ ] **Step 5: 在邮箱来源路由层增加安全包装**

```python
def capture_otp_baseline(email: str):
    if resolve_email_source(email) != "generic_api":
        return None
    from core.generic_api_mail_client import (
        OtpBaseline,
        capture_otp_baseline as capture,
    )
    try:
        return capture(email)
    except Exception:
        if bool(getattr(_email_cfg, "GENERIC_API_REQUIRE_BASELINE", True)):
            raise
        logger.warning("[GenericAPI] 基线抓取失败，严格基线校验已关闭")
        return OtpBaseline(frozenset(), frozenset(), time.time())
```

同时给 `wait_for_otp()` 增加 `otp_baseline=None` 参数，并只在 `source == "generic_api"` 时执行：

```python
if otp_baseline is not None:
    extra_kwargs["otp_baseline"] = otp_baseline
```

- [ ] **Step 6: 写并运行来源路由测试**

```python
def test_capture_baseline_routes_only_generic_api():
    marker = object()
    with patch("core.email_provider.resolve_email_source", return_value="generic_api"), patch(
        "core.generic_api_mail_client.capture_otp_baseline", return_value=marker
    ) as capture:
        assert email_provider.capture_otp_baseline("user@example.com") is marker
    capture.assert_called_once_with("user@example.com")


def test_capture_baseline_skips_non_generic_source():
    with patch("core.email_provider.resolve_email_source", return_value="outlook"):
        assert email_provider.capture_otp_baseline("user@outlook.com") is None
```

Run: `python -m pytest tests/test_generic_api_yangyang.py tests/test_email_provider_gptmail.py -q`

Expected: PASS。

- [ ] **Step 7: 提交基线能力**

```powershell
git add core/generic_api_mail_client.py core/email_provider.py tests/test_generic_api_yangyang.py tests/test_email_provider_gptmail.py
git commit -m "feat: 在发送验证码前记录取码接口基线"
```

---

### Task 4: 在注册流程中使用基线和准确的触发时间

**Files:**
- Modify: `core/roxy_registration.py:2325`
- Test: `tests/test_roxy_registration_otp_retry.py`

**Interfaces:**
- Consumes: `capture_otp_baseline(email)`。
- Consumes: `wait_for_otp(email, after_ts, exclude_codes, otp_baseline)`。
- Guarantees: 首轮使用 `otp_baseline` 判断接口值是否发生变化；`exclude_codes` 只保存页面明确拒绝过的验证码。

- [ ] **Step 1: 写首轮必须排除基线验证码的失败测试**

```python
def test_registration_captures_baseline_before_email_submission():
    events = []
    baseline = OtpBaseline(frozenset({"174510"}), frozenset(), 100.0)

    def capture(_email):
        events.append("baseline")
        return baseline

    def submit(_driver, _email, attempts):
        events.append("submit_email")
        return "otp"

    # 沿用本文件 FakeDriver/FakeClient 和其他注册依赖 patch。
    # wait_for_otp 返回 107902，页面直接 accepted。
    assert events[:2] == ["baseline", "submit_email"]
    assert wait_otp.call_args.kwargs["otp_baseline"] is baseline
    assert wait_otp.call_args.kwargs["exclude_codes"] == set()
```

- [ ] **Step 2: 写基线失败时不得触发发信的测试**

```python
def test_registration_does_not_submit_email_when_required_baseline_fails():
    with patch.object(
        service,
        "capture_otp_baseline",
        side_effect=GenericApiMailError("抓取验证码接口基线失败"),
    ), patch.object(service, "_submit_email_and_wait_next") as submit_email:
        with pytest.raises(GenericApiMailError, match="基线"):
            service.run_roxy_registration("user@example.com", "Test", "2000-01-01")

    submit_email.assert_not_called()
```

- [ ] **Step 3: 运行注册测试并确认失败**

Run: `python -m pytest tests/test_roxy_registration_otp_retry.py -q`

Expected: FAIL，因为注册流程尚未抓取基线。

- [ ] **Step 4: 将基线抓取移动到触发发信前，并初始化排除集合**

```python
baseline = capture_otp_baseline(email)
baseline_codes = set(baseline.codes) if baseline is not None else set()

# 时间戳紧邻触发动作，替代当前在打开登录页之前记录的时间。
otp_after_ts = time.time()
logger.info(
    "[Roxy注册][OTP] 准备触发验证码：after_ts=%s, baseline_codes=%s",
    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(otp_after_ts)),
    sorted(baseline_codes),
)
next_state = _submit_email_and_wait_next(driver, email, attempts=3)

rejected_codes: set[str] = set()
```

基线抓取应在登录页加载完成、准备提交邮箱之前执行；删除当前打开登录页之前的 `otp_after_ts = time.time()`。

- [ ] **Step 5: 统一重发顺序和排除集合**

```python
rejected_codes.add(str(current_otp).strip())
otp_after_ts = time.time()
logger.info(
    "[Roxy注册][OTP] 触发重新发送：after_ts=%s, exclude_codes=%s",
    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(otp_after_ts)),
    sorted(rejected_codes),
)
_click_resend_email_otp(driver, timeout=25)
```

首次轮询和重发轮询都始终传 `otp_baseline=baseline` 与 `exclude_codes=rejected_codes`，即使排除集合为空，避免调用语义因分支不同而变化：

```python
current_otp = wait_for_otp(
    email,
    after_ts=otp_after_ts,
    exclude_codes=rejected_codes,
    otp_baseline=baseline,
)
```

- [ ] **Step 6: 运行注册相关测试**

Run: `python -m pytest tests/test_roxy_registration_otp_retry.py tests/test_roxy_password_setup.py -q`

Expected: PASS；原有“页面拒绝后排除验证码”测试继续通过，首轮新增基线排除测试通过。

- [ ] **Step 7: 提交注册编排改动**

```powershell
git add core/roxy_registration.py tests/test_roxy_registration_otp_retry.py
git commit -m "fix: 注册取码只接受触发后的新验证码"
```

---

### Task 5: 收紧轮询候选、settle 和超时语义

**Files:**
- Modify: `core/generic_api_mail_client.py:574`
- Test: `tests/test_generic_api_yangyang.py`

**Interfaces:**
- Extends: `fetch_latest_otp(..., exclude_codes=None, otp_baseline: OtpBaseline | None = None) -> str`。
- Guarantees: 排除候选永不进入 `best_otp`；未完成 settle 的候选不会在总超时后被返回。

- [ ] **Step 1: 写缓存旧码到新码切换测试**

```python
def test_polling_waits_until_cached_baseline_code_changes():
    session = FakeSequenceSession([
        {"code": "174510"},
        {"code": "174510"},
        {"code": "107902"},
    ])
    account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")

    with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
        "core.generic_api_mail_client.requests.Session", return_value=session
    ), patch("core.generic_api_mail_client.time.sleep"):
        code = fetch_latest_otp(
            account.email,
            max_wait=2,
            poll_interval=1,
            settle_seconds=0,
            exclude_codes={"174510"},
        )

    assert code == "107902"
    assert session.calls == 3
```

- [ ] **Step 2: 写“超时不得返回未完成 settle 候选”的测试**

```python
def test_timeout_does_not_return_unsettled_candidate():
    account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
    session = FakeSingleResponseSession({"code": "107902"})

    with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
        "core.generic_api_mail_client.requests.Session", return_value=session
    ), patch("core.generic_api_mail_client.time.sleep"):
        with pytest.raises(GenericApiMailError, match="settle 未完成"):
            fetch_latest_otp(
                account.email,
                max_wait=0.01,
                poll_interval=1,
                settle_seconds=30,
            )
```

- [ ] **Step 3: 运行测试并确认超时测试失败**

Run: `python -m pytest tests/test_generic_api_yangyang.py -k "cached_baseline or unsettled" -q`

Expected: 第二个测试 FAIL，因为当前第 726-728 行会在总超时后返回 `best_otp`。

- [ ] **Step 4: 只允许通过校验的候选启动 settle**

```python
baseline_hit = False
if otp_baseline is not None and code:
    has_fresh_timestamp = (
        observation.msg_ts is not None
        and after_ts is not None
        and observation.msg_ts + 2 >= after_ts
    )
    has_new_message_id = (
        observation.message_id is not None
        and bool(otp_baseline.message_ids)
        and observation.message_id not in otp_baseline.message_ids
    )
    if not has_fresh_timestamp and not has_new_message_id:
        if observation.message_id and otp_baseline.message_ids:
            baseline_hit = observation.message_id in otp_baseline.message_ids
        else:
            baseline_hit = code in otp_baseline.codes

if code in excluded_codes:
    last_error = f"candidate_excluded: code={code}"
    code = None
elif baseline_hit:
    last_error = f"baseline_unchanged: code={code} id={observation.message_id}"
    code = None

if code is not None:
    _update_settle_candidate(code, observation)
```

将 YangYang 和普通 GenericAPI 的重复候选更新逻辑收敛到 `_update_settle_candidate` 对应的小型内部函数或等价的单一代码路径，确保两条路径执行相同的新鲜度和排除规则。

- [ ] **Step 5: 删除超时返回未确认候选的分支**

```python
if best_otp:
    raise GenericApiMailError(
        f"等待通用 API 验证码超时: {email}; 候选 {best_otp} 的 settle 未完成"
    )
raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")
```

- [ ] **Step 6: 运行 GenericAPI 全部测试**

Run: `python -m pytest tests/test_generic_api_yangyang.py tests/test_generic_api_pool_selection.py -q`

Expected: PASS。

- [ ] **Step 7: 提交轮询语义修复**

```powershell
git add core/generic_api_mail_client.py tests/test_generic_api_yangyang.py
git commit -m "fix: 验证码轮询超时后不再返回未确认候选"
```

---

### Task 6: 完善日志、设计说明和全量回归

**Files:**
- Modify: `core/generic_api_mail_client.py`
- Modify: `docs/superpowers/specs/2026-08-17-generic-api-otp-extraction-design.md`
- Test: all relevant tests

**Interfaces:**
- Produces: 可从单个任务日志还原 `baseline -> trigger -> rejected/accepted candidate -> settle -> submit` 的完整链路。

- [ ] **Step 1: 统一候选日志字段**

每次候选决策输出以下字段：

```python
logger.info(
    "[GenericAPI] OTP候选 decision=%s source=%s code=%s message_id=%s "
    "msg_ts=%s after_ts=%s baseline_hit=%s exclude_hit=%s reason=%s",
    decision,
    observation.source,
    observation.code,
    observation.message_id,
    observation.msg_ts,
    after_ts,
    baseline_hit,
    exclude_hit,
    observation.rejection_reason or "",
)
```

`decision` 只使用 `accept_candidate`、`reject_candidate`、`wait_for_change` 三个固定值，方便日志检索。

- [ ] **Step 2: 更新设计文档中的无时间戳策略**

将原来的“接口没有时间字段时，由严格正文提取、settle 和本次排除集合提供安全性”改为：

```markdown
接口没有时间字段时，settle 不能证明邮件属于本次请求。注册流程必须在触发发信前抓取接口基线，并在轮询时排除基线验证码；基线抓取失败时严格模式直接终止。页面已拒绝的验证码持续加入排除集合，直到获得不同的新候选或等待超时。
```

- [ ] **Step 3: 运行定向测试集**

Run: `python -m pytest tests/test_generic_api_yangyang.py tests/test_generic_api_pool_selection.py tests/test_email_provider_gptmail.py tests/test_roxy_registration_otp_retry.py tests/test_roxy_password_setup.py tests/test_config_defaults.py -q`

Expected: PASS。

- [ ] **Step 4: 运行完整测试集**

Run: `python -m pytest -q`

Expected: 全部 PASS；若出现与当前脏工作区已有改动相关的失败，记录具体测试名、失败堆栈和是否与 OTP 改动相关，不覆盖用户现有修改。

- [ ] **Step 5: 静态检查关键路径**

Run: `rg -n "capture_otp_baseline|OTP_MAX_MESSAGE_AGE_SECONDS|GENERIC_API_REQUIRE_BASELINE|exclude_codes|before_trigger|older_than_max_age|settle 未完成" core config webui tests docs/superpowers`

Expected: 配置、路由、注册调用、轮询实现、测试和设计文档均有对应命中。

- [ ] **Step 6: 人工验收日志**

使用一个 GenericAPI 邮箱启动一次注册，预先确保取码 URL 中存在旧验证码 `A`。期望日志顺序为：

```text
抓取基线 codes=[A]
准备触发验证码 after_ts=...
OTP候选 decision=wait_for_change baseline_hit=True code=A
OTP候选 decision=accept_candidate baseline_hit=False code=B
settle 完成，返回 OTP=B
已提交邮箱验证码
```

点击“重新发送”时，旧验证码 `B` 必须显示为 `exclude_hit=True`，直到接口出现新验证码 `C`；不得再次向页面提交 `B`。

- [ ] **Step 7: 提交文档和日志改动**

```powershell
git add core/generic_api_mail_client.py docs/superpowers/specs/2026-08-17-generic-api-otp-extraction-design.md
git commit -m "docs: 记录通用邮箱验证码新鲜度判定"
```

---

## 验收标准

- 取码接口在触发前和触发后都返回 `174510` 时，程序持续等待，不提交 `174510`。
- 接口随后从 `174510` 更新到 `107902` 时，只提交 `107902`。
- 结构化响应包含早于 `after_ts` 的时间戳时，即使原始 JSON 文本中存在六位数字，也不会通过正则回退重新提取。
- 邮件时间超过 `OTP_MAX_MESSAGE_AGE_SECONDS=3600` 时被拒绝。
- GenericAPI 没有时间戳时，基线抓取成功后仍可通过“验证码发生变化”判断新码。
- 严格模式下基线抓取失败会在触发 OpenAI 发信前停止，并输出明确错误。
- 页面明确拒绝的验证码不会在后续轮询中再次提交。
- 总超时时不会返回尚未完成 settle 的候选。
- 其他邮箱来源的现有行为和测试不变。
