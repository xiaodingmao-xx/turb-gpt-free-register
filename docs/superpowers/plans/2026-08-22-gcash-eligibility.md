# GCash Account Eligibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新版账号页面增加一个安全、可追踪、不会确认付款的 GCash 资格检测任务。

**Architecture:** 复用现有账号 Token、代理路由和后台队列模式；网络检测层创建未确认的 PH/PHP Checkout 会话并读取 Stripe 初始化返回的支付方式，纯解析继续复用 `core/payment_method_detector.py`。数据库只保存脱敏后的派生结果，WebUI 使用独立队列、状态轮询和详细日志展示结果。

**Tech Stack:** Python 3、requests/curl_cffi 现有会话封装、Flask、JSON 账号存储、原生 JavaScript、pytest/unittest。

**Spec:** `docs/superpowers/specs/2026-08-22-gcash-eligibility-design.md`

## Global Constraints

- 只检测用户拥有或明确获授权的账号。
- 不确认支付、不创建 PaymentMethod、不提交付款、不读取 Google Play `purchaseToken`。
- 任何网络失败都显示“未知/检测失败”，不能误判成“无 GCash 资格”。
- 不保存 Token、Cookie、完整 Checkout 响应、Stripe publishable key 或 Checkout Session ID。
- 默认关闭自动检测，必须由新版 WebUI 显式触发。
- 只修改新版 `webui/templates/index.html`，忽略旧版界面。
- 修改采用测试先行：每个生产改动先写失败测试并确认失败，再实现并验证通过。

---

### Task 1: 扩展支付方式规范化，兼容 GCash 标识

**Files:**
- Modify: `core/payment_method_detector.py:34-45`
- Test: `tests/test_payment_method_detector.py`

**Interfaces:**
- Existing `normalize_payment_method_token(value) -> str` 将 `external_gcash` 和 `external_momo` 规范化为 `gcash`、`momo`。
- Existing `classify_payment_method(evidence, expected_method)` 继续返回 `("available"|"unavailable"|"unknown", bool|None)`。

- [ ] **Step 1: Write failing tests**

```python
def test_external_gcash_is_normalized_to_gcash():
    result = detect_oaics(
        {"id": "cs_test_gcash"},
        {"payment_method_types": ["card", "external_gcash"]},
        billing_country="PH",
        fallback_currency="PHP",
        expected_method="gcash",
    )
    assert result["method_status"] == "available"
    assert result["method_available"] is True
    assert result["payment_method_types"] == ["card", "gcash"]
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pytest tests/test_payment_method_detector.py -q`

Expected: FAIL because `external_gcash` is not currently aliased to `gcash`.

- [ ] **Step 3: Implement the minimal aliases**

Add these entries to the existing `aliases` mapping:

```python
"external_gcash": "gcash",
"external_momo": "momo",
```

- [ ] **Step 4: Run all parser tests**

Run: `pytest tests/test_payment_method_detector.py -q`

Expected: PASS with the existing tests and the new GCash test.

- [ ] **Step 5: Commit**

```bash
git add core/payment_method_detector.py tests/test_payment_method_detector.py
git commit -m "feat: normalize GCash payment method"
```

### Task 2: Implement a non-confirming GCash network probe

**Files:**
- Create: `core/gcash_eligibility.py`
- Create: `tests/test_gcash_eligibility.py`

**Interfaces:**
- `check_account_gcash(token: str, *, proxy: str | None = None, timeout: float | None = None, max_attempts: int | None = None, retry_delay: float | None = None, trial_days: int = 0, progress_callback=None) -> dict`
- `format_gcash_phase(phase: str, **fields) -> str`
- `safe_gcash_log_text(value: object, limit: int = 240) -> str`

The result must contain `ok`, `conclusive`, `decision`, `gcash_available`, `trial_eligible`, `actual_trial`, `payment_methods`, `payment_method_status`, `currency`, `amount_due`, `stripe_mode`, `http_status`, `error`, `network_route`, `proxy_used`, and `proxy_ip` when available.

- [ ] **Step 1: Write failing unit tests with fake HTTP sessions**

```python
def test_gcash_available_when_checkout_and_stripe_init_list_gcash(monkeypatch):
    monkeypatch.setattr(
        "core.gcash_eligibility._checkout_session",
        lambda *args, **kwargs: ({
            "checkout_session_id": "cs_test_gcash",
            "one_click_trial_eligible": True,
            "checkout_session": {"subscription_data": {"trial_period_days": 0}},
        }, "pk_test"),
    )
    monkeypatch.setattr(
        "core.gcash_eligibility._stripe_init",
        lambda *args, **kwargs: {
            "mode": "subscription",
            "currency": "php",
            "amount_due": 0,
            "payment_method_types": ["card", "external_gcash"],
        },
    )
    result = check_account_gcash("token", proxy="")
    assert result["decision"] == "available"
    assert result["gcash_available"] is True
    assert result["payment_methods"] == ["card", "gcash"]


def test_network_failure_is_unknown_not_unavailable(monkeypatch):
    monkeypatch.setattr(
        "core.gcash_eligibility._checkout_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("network down")),
    )
    result = check_account_gcash("token", proxy="", max_attempts=1)
    assert result["decision"] == "unknown"
    assert result["gcash_available"] is None
    assert result["conclusive"] is False


def test_gcash_probe_never_logs_token_or_checkout_identifier(monkeypatch):
    messages = []
    monkeypatch.setattr(
        "core.gcash_eligibility._checkout_session",
        lambda *args, **kwargs: ({"checkout_session_id": "cs_secret"}, "pk_secret"),
    )
    monkeypatch.setattr("core.gcash_eligibility._stripe_init", lambda *args, **kwargs: {"payment_method_types": ["gcash"]})
    result = check_account_gcash("eyJsecret-token", proxy="", progress_callback=messages.append)
    rendered = "\n".join(messages)
    assert "eyJsecret-token" not in rendered
    assert "cs_secret" not in rendered
    assert result["decision"] == "available"
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run: `pytest tests/test_gcash_eligibility.py -q`

Expected: FAIL because the module and network probe functions do not exist.

- [ ] **Step 3: Implement the request flow**

Use `core.chatgpt_plan.normalize_token`, `token_claims`, `resolve_plan_check_route`, and `core.session.BrowserSession` for token validation, route selection, and proxy behavior. Add `_checkout_session(session, token, *, trial_days, timeout) -> tuple[dict, str]`, returning the sanitized checkout payload and publishable key, and `_stripe_init(session, checkout_session_id, publishable_key, *, timeout) -> dict`, returning the Stripe initialization payload. Both functions must keep the raw payload in memory only for the duration of the task.

The Checkout JSON must use `country="PH"`, `currency="PHP"`, `plan_name="chatgptplusplan"`, `price_interval="month"`, `seat_quantity=1`, and `checkout_ui_mode="custom"`. Include `subscription_data.trial_period_days` only when `trial_days > 0`. The probe may create an unconfirmed Checkout Session, but must not confirm it or create a payment method.

Map HTTP and response states as follows:

- HTTP 401 or an expired JWT: `credential_invalid`.
- Explicit already-subscribed response: `already_paid`.
- Checkout/Stripe/network/Cloudflare/429 failure: `unknown` with `conclusive=False`.
- Stripe init succeeds and method list contains `gcash` after normalization: `available`.
- Stripe init succeeds with a non-empty method list without GCash: `unavailable`.
- Missing method list: `unknown`, not `unavailable`.

Use `parse_capability_evidence` and `classify_payment_method` from `core.payment_method_detector.py` so nested `payment_method_types`, `ordered_payment_method_types`, and `custom_payment_methods` are handled consistently. Log only phase, HTTP status, method names, country/currency, masked proxy endpoint and error class.

- [ ] **Step 4: Run the unit tests**

Run: `pytest tests/test_gcash_eligibility.py tests/test_payment_method_detector.py -q`

Expected: PASS and no test output contains a token or session identifier.

- [ ] **Step 5: Commit**

```bash
git add core/gcash_eligibility.py tests/test_gcash_eligibility.py core/payment_method_detector.py tests/test_payment_method_detector.py
git commit -m "feat: add non-confirming GCash eligibility probe"
```

### Task 3: Add configuration, queue, persistence, and detailed logs

**Files:**
- Create: `config/gcash.py`
- Create: `core/gcash_check_service.py`
- Modify: `core/db.py:837-1030` for claim, running, recovery, and update functions; `core/db.py:1221-1300` for the lightweight status list
- Create: `tests/test_gcash_check_service.py`

**Interfaces:**
- `config.gcash` exports `GCASH_CHECK_ENABLED`, `GCASH_CHECK_COUNTRY`, `GCASH_CHECK_CURRENCY`, `GCASH_CHECK_TRIAL_DAYS`, `GCASH_CHECK_TIMEOUT`, `GCASH_CHECK_MAX_ATTEMPTS`, `GCASH_CHECK_RETRY_DELAY`, `GCASH_CHECK_WORKERS`, `GCASH_CHECK_QUEUE_LIMIT`.
- `core.gcash_check_service.enqueue_account_gcash_check(account_id: int, email: str, access_token: str, *, trigger: str = "manual") -> dict`.
- `core.gcash_check_service.queue_status() -> dict`.
- `core.gcash_check_service.log_path(email: str) -> Path`.
- DB functions are `claim_account_gcash_check`, `mark_account_gcash_check_running`, `recover_interrupted_gcash_checks`, `update_account_gcash_check`, and `list_account_gcash_check_statuses`.

- [ ] **Step 1: Write failing service and persistence tests**

Test that a successful result writes only derived fields, a network failure preserves `gcash_available=None`, duplicate submissions are rejected, queue snapshots include active/queued/running accounts, and log redaction removes a JWT, proxy credentials, Checkout Session ID, and publishable key.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_gcash_check_service.py -q`

Expected: FAIL because the config, service, and DB methods do not exist.

- [ ] **Step 3: Implement config and queue behavior**

Default values must be `GCASH_CHECK_ENABLED=False`, `GCASH_CHECK_COUNTRY="PH"`, `GCASH_CHECK_CURRENCY="PHP"`, `GCASH_CHECK_TRIAL_DAYS=0`, `GCASH_CHECK_TIMEOUT=20.0`, `GCASH_CHECK_MAX_ATTEMPTS=2`, `GCASH_CHECK_RETRY_DELAY=2.0`, `GCASH_CHECK_WORKERS=1`, and `GCASH_CHECK_QUEUE_LIMIT=100`. Reuse `PLAN_CHECK_PROXY_MODE` and `PLAN_CHECK_PROXY` for routing so the user does not maintain two proxy configurations.

The worker writes `注册日志/gcash-check-<safe-email>.log` with these phases: `worker_start`, `route`, `checkout_request`, `checkout_response`, `stripe_init`, `result`, and `persist`. Use the existing plan-check redaction style and never write the raw response.

- [ ] **Step 4: Implement DB state transitions**

Use the existing JSON account locking and stale-task recovery pattern. Set `gcash_check_status` to `queued`, `running`, `success`, or `failed`; keep the last conclusive result when a later network check is `unknown`; update `gcash_check_result_json` with derived fields only; and expose queue timestamps and retry metadata.

- [ ] **Step 5: Run service, DB, and regression tests**

Run: `pytest tests/test_gcash_check_service.py tests/test_payment_method_detector.py tests/test_plan_check_service.py -q`

Expected: PASS with no existing plan-check behavior changed.

- [ ] **Step 6: Commit**

```bash
git add config/gcash.py core/gcash_check_service.py core/db.py tests/test_gcash_check_service.py
git commit -m "feat: add GCash check queue and persistence"
```

### Task 4: Add current WebUI actions, status polling, and logs

**Files:**
- Modify: `webui/app.py` account API routes and account serialization
- Modify: `webui/templates/index.html` account toolbar, status cell, polling, and log modal
- Modify: `tests/test_webui_account_features.py`

**Interfaces:**
- `POST /api/accounts/<int:acc_id>/gcash-check`
- `POST /api/accounts/gcash-check-bulk` with `{ "account_ids": [1, 2] }`
- `GET /api/accounts/gcash-check-status?ids=1,2`
- `GET /api/accounts/gcash-check-log?email=<urlencoded-email>`

- [ ] **Step 1: Write failing WebUI tests**

Add tests for single and bulk enqueue, invalid/missing account Token, queue snapshot, status serialization without `access_token`, and the current template containing the button, polling endpoint, queue status, status labels, and log action.

- [ ] **Step 2: Run the focused WebUI tests and verify they fail**

Run: `pytest tests/test_webui_account_features.py -q`

Expected: FAIL because the routes and template controls do not exist.

- [ ] **Step 3: Implement the API routes**

Follow the existing live-check and plan-check route patterns. Resolve account IDs through `db.get_account`, pass only the stored access token to the service, return HTTP 202 for accepted tasks, 409 for busy accounts, 503 for a full queue, and 400 for invalid input. Status responses must include only the derived GCash fields and queue information.

- [ ] **Step 4: Implement the新版账号页 UI**

Add a bulk “查 GCash 资格” action, per-account action, a queue badge showing `运行 N · 排队 M`, and a status renderer with `可用`, `不可用`, `未知`, `检测中`, `凭据失效`, and `已订阅`. Poll every 2 seconds for queued/running accounts, merge only returned derived fields, and open the detailed GCash log in the existing modal. Do not change `index_legacy.html`.

- [ ] **Step 5: Run WebUI tests**

Run: `pytest tests/test_webui_account_features.py tests/test_webui_auth.py -q`

Expected: PASS and all account API responses omit access tokens.

- [ ] **Step 6: Commit**

```bash
git add webui/app.py webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: add GCash eligibility controls to account page"
```

### Task 5: Document configuration and perform authorized end-to-end verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Test: existing test suite plus one manually authorized test account

- [ ] **Step 1: Add configuration documentation**

Document the defaults, the distinction between payment-method availability and trial eligibility, the fact that `GCASH_CHECK_ENABLED` defaults to false, and the fields shown in the account page. Explicitly state that Google Play purchase tokens are not used for GCash detection.

- [ ] **Step 2: Run the complete regression suite**

Run: `pytest -q`

Expected: PASS with no changes to registration, live-check, plan-check, password setup, or token export behavior.

- [ ] **Step 3: Perform a no-payment manual verification**

Use one authorized account with a valid access token. Click “查 GCash 资格”, verify the queue and log phases, confirm the result lists `PH/PHP`, confirm no confirm/payment request is sent, and verify that the account page never returns the access token or raw Checkout payload.

- [ ] **Step 4: Review the final diff and commit documentation**

```bash
git diff --check
git status --short
git add .env.example README.md docs/superpowers/specs/2026-08-22-gcash-eligibility-design.md docs/superpowers/plans/2026-08-22-gcash-eligibility.md
git commit -m "docs: document GCash eligibility detection"
```
