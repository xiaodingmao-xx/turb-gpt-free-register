# 浏览器查活队列、Token 状态与邮箱复制实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在默认新版 WebUI 中增加浏览器查活队列可视化、查活后的 Token 状态自动刷新、两个页面的复制邮箱名称按钮，并把 HTTP 403 等查活失败记录为可定位且已脱敏的阶段日志。

**Architecture:** 后端继续使用现有持久化账号状态和浏览器线程池，不引入 SSE、WebSocket 或新依赖。新增 `queue_status()` 从账号的 queued/running 状态生成浏览器队列快照，WebUI 通过轻量 HTTP 接口每 2 秒轮询已提交账号；Roxy 登录链路通过可选诊断回调输出阶段、请求、状态码和安全摘要，数据库仍保持失败时不覆盖旧 Token 的语义。

**Tech Stack:** Python 3、Flask、现有 JSON 账号存储、RoxyBrowser/Selenium、新版 `webui/templates/index.html`、pytest/unittest。

**Spec:** `docs/superpowers/specs/2026-08-21-live-check-queue-token-email-copy-design.md`

## Global Constraints

- 本次只修改默认新版界面，忽略 `webui/templates/index_legacy.html`。
- 浏览器查活前端轮询间隔固定为约 2 秒，并使用 in-flight 保护，不能并发叠加请求。
- `queued` 包含延迟重试任务；`waiting` 只包含可立即等待 worker 的任务；`positions` 不为 delayed 任务分配位置。
- 账号列表、队列接口和日志均不得返回或写入 access token、JWT、Cookie、Authorization、OTP、OAuth code/state、密码或代理凭据。
- 查活成功才写入最新 access token；403、Cloudflare、网络超时、浏览器启动失败和 OTP 超时不能直接标记为 `deactivated`。
- 保留现有复制取件地址、复制整行、复制完整 Token 的行为，不改变 protocol 查活默认路径。
- 每个任务都先写失败测试，再实现最小改动并运行对应测试；每个任务完成后单独提交。

## 文件地图

- `core/live_check_service.py`：增加统一的 `queue_status(mode)` 队列快照，并在入队、重试、终态写回日志中记录队列/Token 写回阶段。
- `core/roxy_live_check.py`：提供 profile 摘要、响应摘要、阶段日志格式和浏览器查活生命周期日志；保留现有 Token 校验和失败分类。
- `core/roxy_registration.py`：为浏览器查活使用的登录函数增加可选诊断回调，把页面加载和 `/api/auth/session` 的 HTTP 状态传回上层；不改变注册流程行为。
- `webui/app.py`：增加 `/api/accounts/live-check-status`，并让批量查活响应返回完整队列快照。
- `webui/templates/index.html`：新版账号页队列摘要、Token 状态轮询、账号页复制邮箱名称、邮箱池页复制邮箱名称。
- `tests/test_live_check_browser_service.py`：队列快照、延迟重试、Token 失败保留和队列日志测试。
- `tests/test_roxy_live_check.py`：403 阶段诊断、响应摘要、profile/代理/认证信息脱敏测试。
- `tests/test_webui_account_features.py`：状态接口、批量接口、新版模板按钮和轮询源码测试。

---

### Task 1: 实现浏览器查活队列快照

**Files:**
- Modify: `core/live_check_service.py:376-386`，在现有 `queue_settings()` 后增加队列快照逻辑。
- Test: `tests/test_live_check_browser_service.py`，增加队列快照和队列日志测试。

**Interfaces:**
- Consumes: `db.list_accounts(limit, offset, archived, sort_key, sort_order)` 返回的账号状态；`_BROWSER_WORKERS`、`_BROWSER_QUEUE_LIMIT`、`_browser_max_attempts()`、`_browser_retry_delays()`。
- Produces: `queue_status(mode: str = "browser") -> dict`，返回 `backend/workers/queue_limit/max_attempts/retry_delays/active/queued/waiting/delayed/available_workers/running_accounts/positions`。

- [ ] **Step 1: Write the failing tests**

```python
def test_browser_queue_status_reports_running_waiting_delayed_and_positions():
    from core import live_check_service as service

    rows = [
        {
            "id": 146, "email": "running@example.com", "live_check_backend": "browser",
            "live_check_status": "running", "live_check_started_at": "2026-08-21T12:00:00",
            "live_check_attempt": 1, "live_check_max_attempts": 3,
        },
        {
            "id": 147, "email": "waiting@example.com", "live_check_backend": "browser",
            "live_check_status": "queued", "live_check_queued_at": "2026-08-21T12:00:01",
            "live_check_next_retry_at": None, "live_check_attempt": 1, "live_check_max_attempts": 3,
        },
        {
            "id": 148, "email": "delayed@example.com", "live_check_backend": "browser",
            "live_check_status": "queued", "live_check_queued_at": "2026-08-21T12:00:02",
            "live_check_next_retry_at": "2099-01-01T00:00:00", "live_check_attempt": 2,
            "live_check_max_attempts": 3,
        },
        {
            "id": 149, "email": "protocol@example.com", "live_check_backend": "protocol",
            "live_check_status": "queued", "live_check_queued_at": "2026-08-21T12:00:03",
        },
    ]
    with patch.object(service.db, "list_accounts", return_value=rows), \
         patch.object(service, "_BROWSER_WORKERS", 2), \
         patch.object(service, "_BROWSER_QUEUE_LIMIT", 100):
        snapshot = service.queue_status("browser")

    assert snapshot["active"] == 1
    assert snapshot["queued"] == 2
    assert snapshot["waiting"] == 1
    assert snapshot["delayed"] == 1
    assert snapshot["available_workers"] == 1
    assert snapshot["running_accounts"] == [{
        "id": 146,
        "email": "running@example.com",
        "started_at": "2026-08-21T12:00:00",
        "attempt": 1,
        "max_attempts": 3,
    }]
    assert snapshot["positions"] == {"147": 1}
    assert "access_token" not in str(snapshot)


def test_browser_queue_status_orders_waiting_accounts_by_queue_time_then_id():
    from core import live_check_service as service

    rows = [
        {"id": 12, "email": "b@example.com", "live_check_backend": "browser",
         "live_check_status": "queued", "live_check_queued_at": "2026-08-21T12:00:01"},
        {"id": 11, "email": "a@example.com", "live_check_backend": "browser",
         "live_check_status": "queued", "live_check_queued_at": "2026-08-21T12:00:01"},
    ]
    with patch.object(service.db, "list_accounts", return_value=rows):
        snapshot = service.queue_status("browser")
    assert snapshot["positions"] == {"11": 1, "12": 2}
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_live_check_browser_service.py -k "queue_status"`

Expected: FAIL because `core.live_check_service.queue_status` does not exist.

- [ ] **Step 3: Implement the minimal queue snapshot**

在 `core/live_check_service.py` 增加以下逻辑：

```python
def _is_future_retry(value: object, now: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed > now
    except (TypeError, ValueError):
        return False


def queue_status(mode: str = "browser") -> dict:
    normalized = normalize_live_check_mode(mode)
    settings = queue_settings(normalized)
    rows = db.list_accounts(
        limit=5000, offset=0, archived="all", sort_key="id", sort_order="asc",
    )
    active_rows = [
        row for row in rows
        if row.get("live_check_backend") == normalized
        and row.get("live_check_status") == "running"
    ]
    queued_rows = [
        row for row in rows
        if row.get("live_check_backend") == normalized
        and row.get("live_check_status") == "queued"
    ]
    now = datetime.now()
    delayed_rows = [row for row in queued_rows if _is_future_retry(row.get("live_check_next_retry_at"), now)]
    waiting_rows = [row for row in queued_rows if row not in delayed_rows]
    waiting_rows.sort(key=lambda row: (str(row.get("live_check_queued_at") or ""), int(row.get("id") or 0)))
    running_accounts = [
        {
            "id": row.get("id"),
            "email": row.get("email"),
            "started_at": row.get("live_check_started_at"),
            "attempt": row.get("live_check_attempt") or 1,
            "max_attempts": row.get("live_check_max_attempts") or settings.get("max_attempts", 1),
        }
        for row in sorted(active_rows, key=lambda item: int(item.get("id") or 0))
    ]
    return {
        "backend": normalized,
        **settings,
        "active": len(running_accounts),
        "queued": len(queued_rows),
        "waiting": len(waiting_rows),
        "delayed": len(delayed_rows),
        "available_workers": max(0, int(settings["workers"]) - len(running_accounts)),
        "running_accounts": running_accounts,
        "positions": {str(row.get("id")): index for index, row in enumerate(waiting_rows, 1)},
    }
```

`_is_future_retry()` 必须安全处理空值、无效 ISO 时间和带 `Z` 的时间；无效值按可立即等待处理。`queue_status()` 只保留上述白名单字段，不能直接把整行账号返回。`queue_settings()` 的既有配置结构保留不变。

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_live_check_browser_service.py -k "queue_status"`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/live_check_service.py tests/test_live_check_browser_service.py
git commit -m "feat: expose live-check queue snapshot"
```

### Task 2: 增加查活状态轮询 API并返回完整队列

**Files:**
- Modify: `webui/app.py:122-200, 891-966`，补充轻量查活字段和状态接口，批量入队响应改用队列快照。
- Test: `tests/test_webui_account_features.py`，增加状态接口、mode 校验和批量响应测试。

**Interfaces:**
- Consumes: Task 1 的 `live_check_service.queue_status(mode)`；现有 `_compact_account_for_list(row)`。
- Produces: `GET /api/accounts/live-check-status?ids=146,147&mode=browser`，返回 `{ok, mode, items, queue}`；空 `ids` 仍返回队列摘要。

- [ ] **Step 1: Write the failing tests**

```python
@patch("webui.app.live_check_service.queue_status")
@patch("webui.app.db.get_account")
def test_live_check_status_returns_compact_account_state_and_queue(self, get_account, queue_status):
    get_account.side_effect = [
        {
            "id": 146, "email": "user@example.com", "access_token": "secret-token",
            "live_check_status": "running", "live_check_backend": "browser",
            "live_check_attempt": 1, "live_check_max_attempts": 3,
        },
    ]
    queue_status.return_value = {
        "backend": "browser", "active": 1, "queued": 5, "waiting": 4,
        "delayed": 1, "available_workers": 0,
        "running_accounts": [{"id": 146, "email": "user@example.com"}],
        "positions": {"147": 1},
    }

    response = self.client.get("/api/accounts/live-check-status?ids=146&mode=browser")

    self.assertEqual(response.status_code, 200)
    payload = response.get_json()
    self.assertEqual(payload["items"][0]["live_check_status"], "running")
    self.assertTrue(payload["items"][0]["has_access_token"])
    self.assertNotIn("access_token", payload["items"][0])
    self.assertEqual(payload["queue"]["queued"], 5)
    queue_status.assert_called_once_with("browser")


def test_live_check_status_rejects_invalid_mode(self):
    response = self.client.get("/api/accounts/live-check-status?mode=auto")
    self.assertEqual(response.status_code, 400)


@patch("webui.app.live_check_service.queue_status")
@patch("webui.app.live_check_service.enqueue_account_live_check")
@patch("webui.app.db.get_account")
def test_browser_live_check_bulk_returns_queue_snapshot(self, get_account, enqueue, queue_status):
    get_account.return_value = {"id": 7, "email": "user@example.com"}
    enqueue.return_value = {"accepted": True, "account_id": 7, "email": "user@example.com"}
    queue_status.return_value = {"backend": "browser", "active": 1, "queued": 1}

    response = self.client.post(
        "/api/accounts/check-live-bulk",
        json={"account_ids": [7], "mode": "browser"},
    )

    self.assertEqual(response.status_code, 202)
    self.assertEqual(response.get_json()["queue"]["queued"], 1)
    queue_status.assert_called_once_with("browser")
```

同步修改原有 `test_browser_live_check_bulk_forwards_explicit_mode`，将 mock 从 `queue_settings` 换成 `queue_status`，其余 enqueue 参数断言保持不变。

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_webui_account_features.py -k "live_check_status or browser_live_check_bulk"`

Expected: FAIL because the GET route不存在，且批量接口仍调用 `queue_settings()`。

- [ ] **Step 3: Implement the API and compact fields**

在 `_compact_account_for_list()` 的固定/可选轻量字段中加入 `live_check_ok`、`live_check_trigger`、`live_check_queued_at`、`live_check_started_at`、`live_check_completed_at`、`live_check_attempt`、`live_check_max_attempts`、`live_check_next_retry_at`；保留 `has_access_token` 布尔值，绝不加入 `access_token`。

在账号状态路由附近增加：

```python
@app.get("/api/accounts/live-check-status")
def api_accounts_live_check_status():
    try:
        mode = live_check_service.normalize_live_check_mode(request.args.get("mode") or "browser")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    raw_ids = str(request.args.get("ids") or "").strip()
    ids = []
    for raw in raw_ids.split(",") if raw_ids else []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    items = []
    for acc_id in dict.fromkeys(ids):
        row = db.get_account(acc_id)
        if row:
            items.append(_compact_account_for_list(row))
    return jsonify({
        "ok": True,
        "mode": mode,
        "items": items,
        "queue": live_check_service.queue_status(mode),
    })
```

把 `/api/accounts/check-live-bulk` 返回体的 `queue` 从 `queue_settings(mode)` 改成 `queue_status(mode)`，保证提交响应立即含有当前执行账号、排队数、延迟数和位置。

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_webui_account_features.py -k "live_check_status or browser_live_check_bulk"`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add webui/app.py tests/test_webui_account_features.py
git commit -m "feat: add live-check status polling API"
```

### Task 3: 完善 Roxy 浏览器查活阶段日志和 403 诊断

**Files:**
- Modify: `core/roxy_live_check.py:41-69, 149-328`，增加安全摘要和阶段日志。
- Modify: `core/roxy_registration.py:147-219, 2252-2404`，为查活登录增加可选诊断回调和 session probe 状态返回。
- Test: `tests/test_roxy_live_check.py`，增加响应摘要、403 阶段和 profile 脱敏测试。
- Test: `tests/test_live_check_browser_service.py`，增加 token_persist/retry 诊断日志断言。

**Interfaces:**
- Consumes: 现有 `progress_callback`、`safe_url_for_log()`、`safe_error_text()`、`RoxyExistingLoginError`。
- Produces: `safe_profile_hint(value: object) -> str`、`safe_response_summary(value: object, limit: int = 160) -> str`、`format_browser_phase(phase: str, **fields) -> str`；`login_existing_account_with_otp(driver, email, *, progress_callback=None) -> dict` 保持旧调用兼容。

- [ ] **Step 1: Write the failing tests**

```python
def test_safe_response_summary_removes_html_and_session_secrets():
    from core.roxy_live_check import safe_response_summary

    summary = safe_response_summary(
        '<html><title>Cloudflare</title><body>accessToken=secret-token '
        'code=secret-code state=secret-state</body></html>'
    )

    assert len(summary) <= 160
    assert "Cloudflare" in summary
    assert "secret-token" not in summary
    assert "secret-code" not in summary
    assert "secret-state" not in summary
    assert "<html>" not in summary


def test_browser_phase_formatter_contains_403_diagnostics_without_profile_id():
    from core.roxy_live_check import format_browser_phase, safe_profile_hint

    line = format_browser_phase(
        "session_probe", request="GET /api/auth/session", host="chatgpt.com",
        http_status=403, route="proxy", proxy="socks5://proxy.example:1080",
        profile_hint=safe_profile_hint("saved-profile"),
        response_summary="Cloudflare challenge", retryable=True,
    )

    assert "phase=session_probe" in line
    assert "request=GET /api/auth/session" in line
    assert "http_status=403" in line
    assert "route=proxy" in line
    assert "saved-profile" not in line
    assert "retryable=true" in line


def test_session_probe_progress_reports_http_403():
    from core import roxy_registration

    driver = Mock()
    driver.execute_async_script.return_value = {
        "ok": False, "http_status": 403, "content_type": "text/html",
        "title": "Cloudflare", "summary": "Cloudflare challenge accessToken=secret",
    }
    messages = []

    result = roxy_registration._read_chatgpt_session_once(
        driver, progress_callback=messages.append,
    )

    assert result is None
    rendered = "\n".join(messages)
    assert "phase=session_probe" in rendered
    assert "http_status=403" in rendered
    assert "secret" not in rendered
```

扩展现有 `test_browser_live_check_progress_never_contains_session_secrets()`，要求所有进度行仍不包含 access token、callback code/state；新增断言包含 `phase=profile_open`、`phase=driver_start`、`phase=session_validate` 或 `phase=terminal`。

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_roxy_live_check.py -k "response_summary or phase_formatter or session_probe"`

Expected: FAIL because the new safety/phase functions和 `progress_callback` 参数尚不存在。

- [ ] **Step 3: Implement safe diagnostic helpers**

在 `core/roxy_live_check.py` 中：

```python
def safe_profile_hint(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def safe_response_summary(value: object, limit: int = 160) -> str:
    text = safe_error_text(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max(1, int(limit))]


def format_browser_phase(phase: str, **fields) -> str:
    parts = [f"[浏览器查活] phase={str(phase or 'unknown').strip() or 'unknown'}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        rendered = safe_response_summary(value, limit=160)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)
```

`safe_error_text()` 要继续移除 URL query、Cookie、Authorization、Token、OTP、`code`、`state` 和代理认证字段；profile 只通过 `safe_profile_hint()` 的摘要写入日志。`format_browser_phase()` 的 request/path 只能经过 `safe_url_for_log()` 或固定的脱敏 path。

- [ ] **Step 4: Implement phase logging and session probe plumbing**

在 `check_account_liveness_with_roxy()` 中按以下顺序输出阶段：`profile_open`、`driver_start`、`page_load`、`login_start`、`session_validate`、`cleanup`、`terminal`。日志内容至少包含 account id、profile source、脱敏 profile hint、host/path、route/proxy、耗时或结果、failure kind 和 retryable。

在 `core/roxy_registration.py` 中只增加可选参数，不改变现有注册调用：

```python
def _read_chatgpt_session_once(
    driver, *, progress_callback=None, probe_callback=None,
) -> dict | None:
    result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('/api/auth/session', {credentials: 'include'})
          .then(async response => {
            const raw = await response.text();
            let data = {};
            try { data = raw ? JSON.parse(raw) : {}; } catch (_) {}
            done({
              ok: response.ok,
              http_status: response.status,
              content_type: response.headers.get('content-type') || '',
              title: document.title || '',
              summary: raw.replace(/\\s+/g, ' ').slice(0, 160),
              data,
            });
          })
          .catch(error => done({ok: false, http_status: 0, summary: String(error)}));
    """)
    if not isinstance(result, dict):
        if probe_callback:
            probe_callback({"http_status": 0, "summary": "driver returned no result"})
        if progress_callback:
            progress_callback("[浏览器查活] phase=session_probe request=GET /api/auth/session http_status=unknown response_summary=driver returned no result retryable=true")
        return None
    if probe_callback:
        probe_callback(result)
    if progress_callback and int(result.get("http_status") or 0) >= 400:
        summary = re.sub(r"<[^>]+>", " ", str(result.get("summary") or result.get("title") or ""))
        summary = re.sub(r"(?i)(accessToken|authorization|cookie|password|token|otp|code|state)\\s*[:=]\\s*[^\\s,}]+", r"\\1=<redacted>", summary)
        summary = re.sub(r"\\s+", " ", summary).strip()[:160]
        progress_callback(
            "[浏览器查活] phase=session_probe request=GET /api/auth/session "
            f"http_status={result.get('http_status')} content_type={result.get('content_type') or '-'} "
            f"response_summary={summary or '-'} retryable=true"
        )
    if result.get("ok") and isinstance(result.get("data"), dict):
        data = result["data"]
        if data.get("accessToken"):
            return data
    return None


def _fetch_chatgpt_session(
    driver, timeout=90, auto_jump_wait=15, *, progress_callback=None,
) -> dict:
    last_probe = {"http_status": None, "summary": "session 暂无 accessToken"}
    def remember_probe(value):
        if isinstance(value, dict):
            last_probe.update({
                "http_status": value.get("http_status"),
                "summary": value.get("summary") or value.get("title") or "session 暂无 accessToken",
            })
    end = time.time() + timeout
    while time.time() < end:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" in current:
            data = _read_chatgpt_session_once(
                driver, progress_callback=progress_callback, probe_callback=remember_probe,
            )
            if data:
                return data
        time.sleep(2)
    if int(last_probe.get("http_status") or 0) == 403:
        raise RoxyExistingLoginError(
            "cloudflare_blocked", "session_probe http_status=403，登录态接口被拒绝",
            retryable=True,
        )
    raise RuntimeError("等待 /api/auth/session accessToken 超时，最后响应未取得登录态")


def login_existing_account_with_otp(
    driver, email: str, *, progress_callback=None,
) -> dict:
    progress = progress_callback or (lambda message: None)
    progress("[浏览器查活] phase=login_start status=started")
    _safe_get(driver, "https://chatgpt.com/auth/login", accept_hosts=("chatgpt.com", "auth.openai.com"))
    progress("[浏览器查活] phase=page_load host=chatgpt.com path=/auth/login status=completed")
    existing = _read_chatgpt_session_once(driver, progress_callback=progress)
    if existing:
        return existing
    # 下面继续使用现有 OTP 输入、提交和重发逻辑；每个阶段只通过 progress 输出脱敏摘要。
    return _fetch_chatgpt_session(driver, timeout=90, progress_callback=progress)
```

执行时保留当前函数中的 OTP、重发、页面状态判断和窗口切换逻辑，只把 `progress_callback` 传入初次 session probe 和最终 `_fetch_chatgpt_session()`；不得用上面的结构重写或删除现有登录分支。

`/api/auth/session` 的浏览器脚本必须返回 `http_status`、`content_type`、页面标题和最多 160 字符的安全摘要，但不能把完整响应体、Token 或 Cookie 传入日志；Python 侧在写日志前再次调用 `safe_error_text()`。遇到 403 时记录 `phase=session_probe request=GET /api/auth/session http_status=403`，并根据摘要标记 `cloudflare_blocked` 或 `access_denied`，两者均保持 `retryable=true`，不标记 `deactivated`。连续等待 session 超时的错误也要带上最后一次 probe 的状态和摘要。

在 `_mark_browser_terminal()` 和 `_schedule_browser_retry()` 增加 `phase=token_persist`、`phase=retry`、`attempt=x/y`、`next_retry_at`、`failure_kind`、`retryable`；成功日志只写“已写回 Token 字段”，不写 Token 内容。

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_roxy_live_check.py tests/test_live_check_browser_service.py -k "safe_ or phase or progress or queue or failed_browser or retry"`

Expected: PASS，且失败结果仍保留旧 access token。

- [ ] **Step 6: Commit**

```bash
git add core/roxy_live_check.py core/roxy_registration.py core/live_check_service.py tests/test_roxy_live_check.py tests/test_live_check_browser_service.py
git commit -m "feat: add detailed browser live-check diagnostics"
```

### Task 4: 新版账号页显示队列并轮询 Token 状态

**Files:**
- Modify: `webui/templates/index.html:2080-2090, 2443-2465, 3466-3491, 3866-3952, 4680-4721`。
- Test: `tests/test_webui_account_features.py`，增加新版队列元素、2 秒轮询、状态合并和停止定时器的源码断言。

**Interfaces:**
- Consumes: Task 2 的 `/api/accounts/live-check-status`，返回 `items` 和 `queue`。
- Produces: `startBrowserLiveCheckPolling(ids)`、`pollBrowserLiveCheckStatuses()`、`renderBrowserLiveCheckQueue(queue)`；只更新 `ACCOUNTS` 中的轻量字段。

- [ ] **Step 1: Write the failing tests**

```python
def test_account_template_contains_browser_queue_and_token_polling(self):
    template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")

    self.assertIn('id="browserLiveCheckQueueStatusV2"', html)
    self.assertIn("/api/accounts/live-check-status?ids=", html)
    self.assertIn("function pollBrowserLiveCheckStatuses", html)
    self.assertIn("function startBrowserLiveCheckPolling", html)
    self.assertIn("setInterval(pollBrowserLiveCheckStatuses, 2000)", html)
    self.assertIn("browserLiveCheckPollingIds.delete", html)
    self.assertIn("clearInterval(browserLiveCheckTimer)", html)
    self.assertIn("renderBrowserLiveCheckQueue", html)


def test_account_template_uses_started_ids_for_browser_polling(self):
    template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    self.assertIn("startBrowserLiveCheckPolling((r.started || []).map(item => item.id))", html)
    self.assertIn("Object.assign(row, next)", html)
    self.assertIn("live_check_status", html)
    self.assertIn("has_access_token", html)
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_webui_account_features.py -k "browser_queue or_browser_polling"`

Expected: FAIL because新版模板没有浏览器队列元素和轮询函数。

- [ ] **Step 3: Implement the account-page queue and polling UI**

在新版账号工具栏增加：

```html
<span class="acc-v2-selected" id="browserLiveCheckQueueStatusV2" title="Roxy 浏览器查活队列状态">浏览器查活：空闲</span>
```

在现有密码/2FA polling 状态变量旁增加：

```javascript
let browserLiveCheckTimer = null;
let browserLiveCheckPollingIds = new Set();
let browserLiveCheckPollInFlight = false;
```

实现队列渲染和轮询：

```javascript
function renderBrowserLiveCheckQueue(queue = {}) {
  const el = $('#browserLiveCheckQueueStatusV2');
  if (!el) return;
  const running = (queue.running_accounts || []).map(row => row.email).filter(Boolean);
  const current = running.length ? `执行 ${running.join('、')}` : '执行 空闲';
  el.textContent = `浏览器查活：${current} · 排队 ${Number(queue.queued || 0)}`;
  el.title = `等待 ${Number(queue.waiting || 0)} · 延迟 ${Number(queue.delayed || 0)} · 可用 worker ${Number(queue.available_workers || 0)}`;
}

async function pollBrowserLiveCheckStatuses() {
  if (browserLiveCheckPollInFlight || !browserLiveCheckPollingIds.size) return;
  browserLiveCheckPollInFlight = true;
  const ids = Array.from(browserLiveCheckPollingIds);
  try {
    const result = await api(`/api/accounts/live-check-status?ids=${encodeURIComponent(ids.join(','))}&mode=browser`);
    renderBrowserLiveCheckQueue(result.queue || {});
    const byId = new Map((result.items || []).map(row => [Number(row.id), row]));
    ACCOUNTS.forEach(row => {
      const next = byId.get(Number(row.id));
      if (next) Object.assign(row, next);
    });
    ids.forEach(id => {
      const next = byId.get(Number(id));
      if (!next || !['queued', 'running'].includes(String(next.live_check_status || ''))) {
        browserLiveCheckPollingIds.delete(Number(id));
      }
    });
    renderAccounts();
  } catch (_) {
    // 下一轮继续请求，保留当前页面状态。
  } finally {
    browserLiveCheckPollInFlight = false;
    if (!browserLiveCheckPollingIds.size && browserLiveCheckTimer) {
      clearInterval(browserLiveCheckTimer);
      browserLiveCheckTimer = null;
      renderBrowserLiveCheckQueue({});
    }
  }
}

function startBrowserLiveCheckPolling(ids = []) {
  ids.map(Number).filter(Number.isFinite).forEach(id => browserLiveCheckPollingIds.add(id));
  if (!browserLiveCheckPollingIds.size) return;
  if (!browserLiveCheckTimer) browserLiveCheckTimer = setInterval(pollBrowserLiveCheckStatuses, 2000);
  pollBrowserLiveCheckStatuses();
}
```

在 `checkSelectedLive()` 中，浏览器模式成功入队后调用 `startBrowserLiveCheckPolling((r.started || []).map(item => item.id))`，并先用 `renderBrowserLiveCheckQueue(r.queue || {})` 显示提交响应的快照。轮询只使用 started ID，不把 busy/skipped 账号加入集合；`_tokenCellV2()` 继续根据 `has_access_token`、`live_check_status`、`live_check_error` 渲染，合并后的字段会自动刷新 Token 和查活显示。

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_webui_account_features.py -k "browser_queue or_browser_polling"`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: poll browser live-check queue and token state"
```

### Task 5: 在账号页和邮箱池页增加复制邮箱名称

**Files:**
- Modify: `webui/templates/index.html:3597-3608, 4034-4049, 5124-5150, 5207-5241`。
- Test: `tests/test_webui_account_features.py`，增加两个新版邮箱列按钮和事件委托断言。

**Interfaces:**
- Consumes: 账号表格已有 `ACCOUNTS` 的 `email` 字段，邮箱池已有 `OUTLOOK` 的 `email` 字段和 `copyText()`。
- Produces: 账号按钮 `data-account-copy-email`、邮箱池按钮 `data-pool-copy-email`；两者复制完整邮箱地址并显示“邮箱名称已复制”。

- [ ] **Step 1: Write the failing tests**

```python
def test_account_and_pool_templates_expose_copy_email_name_actions(self):
    template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")

    self.assertIn("data-account-copy-email", html)
    self.assertIn("data-pool-copy-email", html)
    self.assertGreaterEqual(html.count("复制邮箱名称"), 2)
    self.assertIn("复制取件地址", html)
    self.assertIn("复制整行", html)


def test_pool_template_keeps_full_line_copy_action(self):
    template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    self.assertIn("cbtn('复制邮箱', r.copy_line", html)
    self.assertIn("copyText(email)", html)
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_webui_account_features.py -k "copy_email or_pool_template"`

Expected: FAIL because新版模板还没有两个 `data-*` 复制邮箱名称动作。

- [ ] **Step 3: Implement both copy actions**

账号邮箱单元格保留取件地址按钮并加入：

```javascript
<button type="button" class="acc-v2-email-copy" data-account-copy-email="${esc(r.id)}">复制邮箱名称</button>
```

在 `onAccountsBodyClick()` 的取件地址处理前增加事件委托：根据 `data-account-copy-email` 找到 `ACCOUNTS` 中对应行的完整 `email`，调用 `await copyText(email)`，成功后提示“邮箱名称已复制”，失败提示“复制邮箱名称失败”；不调用 secret API。

邮箱池邮箱单元格加入：

```javascript
<button type="button" class="acc-v2-email-copy" data-pool-copy-email="${email}">复制邮箱名称</button>
```

在 `onOutlookBodyClick()` 的 `data-pool-act` 分支前处理该按钮，直接复制 `t.dataset.poolCopyEmail`。保持 `cbtn('复制邮箱', r.copy_line, 'primary')`，使原来的复制整行素材继续可用；所有属性值继续经过 `esc()`。

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_webui_account_features.py -k "copy_email or_pool_template"`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: add copy email name actions"
```

### Task 6: 集成回归、脱敏审计与完成前验证

**Files:**
- Verify only: `core/live_check_service.py`, `core/roxy_live_check.py`, `core/roxy_registration.py`, `webui/app.py`, `webui/templates/index.html`。
- Verify unchanged: `webui/templates/index_legacy.html`。

**Interfaces:**
- Consumes: Tasks 1-5 的实现和测试。
- Produces: 全部相关测试通过，工作区只包含本功能允许的现代界面和后端文件变更。

- [ ] **Step 1: Run all focused regression tests**

Run: `pytest -q tests/test_live_check_browser_service.py tests/test_roxy_live_check.py tests/test_webui_account_features.py`

Expected: PASS，且无日志脱敏测试失败。

- [ ] **Step 2: Run the full test suite**

Run: `pytest -q`

Expected: PASS；如果环境中存在与本功能无关的失败，记录准确的测试名和首个失败原因，不跳过本功能测试。

- [ ] **Step 3: Check formatting and legacy UI boundary**

Run: `git diff --check`

Expected: 无输出。

Run: `git status --short; git diff --name-only -- webui/templates/index_legacy.html`

Expected: 旧版模板路径无 diff；修改文件只来自文件地图中的项目文件。

- [ ] **Step 4: Audit sensitive output**

Run: `rg -n "accessToken=|Authorization:|Cookie:|proxyPassword|proxyUserName|otp=|code=|state=|password=" core/live_check_service.py core/roxy_live_check.py core/roxy_registration.py`

Expected: 查活新增日志模板中没有把敏感值拼入输出；若匹配的是请求构造或已有安全脱敏代码，逐处确认不是日志写入路径。

- [ ] **Step 5: Commit the integrated result**

```bash
git add core/live_check_service.py core/roxy_live_check.py core/roxy_registration.py webui/app.py webui/templates/index.html tests/test_live_check_browser_service.py tests/test_roxy_live_check.py tests/test_webui_account_features.py
git commit -m "feat: improve browser live-check observability"
```

## Self-Review

1. **Spec coverage:**
   - 队列运行账号、排队数量、delayed/waiting/positions：Task 1、Task 2、Task 4。
   - 查活后的 Token 状态写回和前端刷新：Task 2、Task 3、Task 4。
   - 403 阶段、请求、host/path、线路、profile、响应摘要、重试决策和脱敏：Task 3、Task 6。
   - 账号页与邮箱池页复制邮箱名称，同时保留取件地址和复制整行：Task 5。
   - 忽略旧版界面和完整回归验证：Global Constraints、Task 6。

2. **Placeholder scan:** 计划中的每一步都有目标文件、函数名、测试命令、预期结果或具体代码结构；没有未定义的后续动作。

3. **Type consistency:** Task 1 产出的 `queue_status(mode) -> dict` 被 Task 2 的 Flask 路由和 Task 4 的 `result.queue` 直接消费；Task 2 产出的 `items` 继续使用 `_compact_account_for_list()`，Task 4 只合并轻量字段；Task 3 的安全函数名称和 `login_existing_account_with_otp(driver, email, *, progress_callback=None) -> dict` 与现有调用兼容。
