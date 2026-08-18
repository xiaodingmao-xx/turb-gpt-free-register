# Roxy Browser Live Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit `browser` live-check mode that opens a Roxy browser, reuses or establishes the target account session with email OTP, validates identity, and safely refreshes the stored ChatGPT access token.

**Architecture:** Keep the existing protocol live check as the backward-compatible default. Add a focused `core/roxy_live_check.py` backend, dispatch it through the existing live-check claim/status model with a separate single-worker browser executor, and persist backend/failure/profile diagnostics through `db.update_account_liveness()`.

**Tech Stack:** Python 3, Flask, Selenium-compatible RoxyBrowser driver, `ThreadPoolExecutor`, JSON account storage, existing email-provider OTP APIs, `unittest`/`pytest`.

**Spec:** `docs/superpowers/specs/2026-08-18-roxy-browser-live-check-design.md`

## Global Constraints

- Preserve `protocol` as the default when callers omit `mode`.
- First release supports only `protocol` and `browser`; do not add automatic backend fallback.
- Browser live check must not set/reset passwords, create accounts, or complete profile pages.
- Only explicit account-unusable evidence may produce `status=deactivated`.
- Failure must preserve the account's previous `access_token`, identity, plan, and password-setup fields.
- Validate session email, JWT email, session user id, stored user id, and token expiration before persistence.
- Historical Roxy profiles are closed but never deleted; only task-created temporary profiles may be deleted.
- Browser live-check concurrency defaults to one worker, with bounded delayed retries that do not occupy a worker.
- Never log or return OTP values, access tokens, Cookie values, OAuth callback code/state, proxy credentials, or account passwords.
- Reuse the current account JSON/export mechanism; do not introduce a database migration framework.
- Preserve all existing uncommitted workspace changes. Inspect overlapping diffs before editing and stage only task-owned hunks/files.
- Follow strict TDD: run each focused test and observe the expected failure before production edits.

---

## File Responsibility Map

- `core/roxy_live_check.py`: browser live-check result model, identity validation, profile lifecycle, error classification, and Roxy orchestration.
- `core/roxy_registration.py`: existing-account OTP browser login primitive and reusable ChatGPT session readers.
- `core/live_check_service.py`: mode normalization, protocol/browser dispatch, separate browser executor, queue slots, and delayed retry scheduling.
- `core/db.py`: live-check claim/requeue/final-result metadata and token-preserving persistence.
- `config/roxybrowser.py`: browser live-check worker, queue, retry, and temporary-profile settings.
- `config/__init__.py`: top-level compatibility exports for the new settings.
- `webui/app.py`: validate and forward `mode` for bulk live-check requests.
- `webui/config_editor.py`: expose browser live-check settings.
- `webui/templates/index.html`: explicit protocol/browser actions and backend/failure display.
- `tests/test_roxy_live_check.py`: identity, OTP login, profile lifecycle, redaction, and cleanup behavior.
- `tests/test_live_check_browser_service.py`: DB metadata, dispatch, queue ownership, and retry behavior.
- `tests/test_webui_account_features.py`: API and account-page behavior.
- `tests/test_config_defaults.py`: configuration defaults and editor coverage.
- `README.md`: operator-facing browser live-check usage and constraints.

---

### Task 1: Add Browser Live-Check Configuration and Persistent State

**Files:**
- Modify: `config/roxybrowser.py:111-148`
- Modify: `config/__init__.py:1-107,220-260`
- Modify: `webui/config_editor.py:270-320`
- Modify: `core/db.py:1554-1665`
- Modify: `tests/test_config_defaults.py`
- Create: `tests/test_live_check_browser_service.py`
- Test: `tests/test_account_list_query.py`

**Interfaces:**
- Produces `LIVE_CHECK_BROWSER_WORKERS: int`, `LIVE_CHECK_BROWSER_QUEUE_LIMIT: int`, `LIVE_CHECK_BROWSER_MAX_ATTEMPTS: int`, `LIVE_CHECK_BROWSER_RETRY_DELAYS: str`, and `LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE: bool`.
- Changes `db.claim_account_live_check(acc_id: int, trigger: str = "manual", *, backend: str = "protocol", max_attempts: int = 1) -> bool`.
- Produces `db.requeue_account_live_check(acc_id: int, error: str, *, failure_kind: str, attempt: int, max_attempts: int, next_retry_at: str) -> bool`.
- Extends `db.update_account_liveness(acc_id: int, result: dict | None = None) -> bool` with backend/failure/profile metadata while preserving existing success behavior.

- [ ] **Step 1: Write failing configuration tests**

Add to `tests/test_config_defaults.py`:

```python
def test_browser_live_check_defaults_are_safe_and_serial():
    from config import roxybrowser

    assert roxybrowser.LIVE_CHECK_BROWSER_WORKERS == 1
    assert roxybrowser.LIVE_CHECK_BROWSER_QUEUE_LIMIT == 100
    assert roxybrowser.LIVE_CHECK_BROWSER_MAX_ATTEMPTS == 3
    assert roxybrowser.LIVE_CHECK_BROWSER_RETRY_DELAYS == "15,60,180"
    assert roxybrowser.LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE is True


def test_config_editor_exposes_browser_live_check_settings():
    from webui.config_editor import EDITABLE_FIELDS

    fields = {item["key"]: item for item in EDITABLE_FIELDS}
    expected = {
        "LIVE_CHECK_BROWSER_WORKERS": "int",
        "LIVE_CHECK_BROWSER_QUEUE_LIMIT": "int",
        "LIVE_CHECK_BROWSER_MAX_ATTEMPTS": "int",
        "LIVE_CHECK_BROWSER_RETRY_DELAYS": "str",
        "LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE": "bool",
    }
    assert {key: fields[key]["type"] for key in expected} == expected
```

- [ ] **Step 2: Run the configuration tests and observe the missing attributes**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config_defaults.py -k "browser_live_check" -q
```

Expected: FAIL because `LIVE_CHECK_BROWSER_WORKERS` and the editor fields do not exist.

- [ ] **Step 3: Add configuration values and environment overrides**

Add to `config/roxybrowser.py` immediately after password-setup queue settings:

```python
# Roxy 真实浏览器查活使用独立队列。默认单并发，避免同时启动多个登录窗口。
LIVE_CHECK_BROWSER_WORKERS: int = 1
LIVE_CHECK_BROWSER_QUEUE_LIMIT: int = 100
LIVE_CHECK_BROWSER_MAX_ATTEMPTS: int = 3
# 逗号分隔；第 N 次失败后使用第 N 个延迟，超出时使用最后一个值。
LIVE_CHECK_BROWSER_RETRY_DELAYS: str = "15,60,180"
# 只控制查活任务本次创建的临时 profile；历史 profile 永不删除。
LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE: bool = True
```

Extend the existing `apply_env_overrides` mapping with:

```python
'LIVE_CHECK_BROWSER_WORKERS': 'int',
'LIVE_CHECK_BROWSER_QUEUE_LIMIT': 'int',
'LIVE_CHECK_BROWSER_MAX_ATTEMPTS': 'int',
'LIVE_CHECK_BROWSER_RETRY_DELAYS': 'str',
'LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE': 'bool',
```

Expose the same five names from `config/__init__.py`'s `config.roxybrowser` import block and `__all__` list. Add five editor entries to `webui/config_editor.py` under the RoxyBrowser group with labels “浏览器查活并发数”, “浏览器查活队列容量”, “浏览器查活最大尝试”, “浏览器查活退避秒数”, and “删除查活临时环境”.

- [ ] **Step 4: Run the configuration tests and confirm they pass**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_config_defaults.py -k "browser_live_check" -q
```

Expected: PASS.

- [ ] **Step 5: Write failing database state tests**

Add to `tests/test_live_check_browser_service.py`:

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from core import db


def _db_patchers(root: Path):
    return [
        patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
        patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
        patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
        patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
        patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
    ]


def test_browser_claim_and_requeue_persist_attempt_metadata():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "user@example.com", "access_token": "old-token"}]),
            encoding="utf-8",
        )
        patchers = _db_patchers(root)
        try:
            for item in patchers:
                item.start()
            assert db.claim_account_live_check(
                1, trigger="manual", backend="browser", max_attempts=3
            )
            assert db.requeue_account_live_check(
                1,
                "TimeoutException: page load timeout",
                failure_kind="network_unavailable",
                attempt=2,
                max_attempts=3,
                next_retry_at="2026-08-18T12:00:15",
            )
            row = db.get_account(1)
        finally:
            for item in reversed(patchers):
                item.stop()

    assert row["live_check_status"] == "queued"
    assert row["live_check_backend"] == "browser"
    assert row["live_check_attempt"] == 2
    assert row["live_check_max_attempts"] == 3
    assert row["live_check_failure_kind"] == "network_unavailable"
    assert row["live_check_next_retry_at"] == "2026-08-18T12:00:15"
    assert row["access_token"] == "old-token"


def test_failed_browser_live_check_preserves_old_token_and_records_diagnostics():
    rows = [{"id": 1, "email": "user@example.com", "access_token": "old-token"}]
    result = {
        "ok": False,
        "status": "failed",
        "backend": "browser",
        "failure_kind": "profile_account_mismatch",
        "profile_id": "saved-profile",
        "profile_source": "saved",
        "proxy_used": "socks5://proxy.example:1080",
        "error": "Roxy profile 登录邮箱与目标账号不一致",
    }
    with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
        assert db.update_account_liveness(1, result)

    assert rows[0]["access_token"] == "old-token"
    assert rows[0]["live_check_backend"] == "browser"
    assert rows[0]["live_check_failure_kind"] == "profile_account_mismatch"
    assert rows[0]["live_check_profile_id"] == "saved-profile"
    assert rows[0]["live_check_profile_source"] == "saved"
```

- [ ] **Step 6: Run the database tests and observe signature/field failures**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_live_check_browser_service.py -q
```

Expected: FAIL because `claim_account_live_check` lacks `backend`, `requeue_account_live_check` is missing, and diagnostics are not persisted.

- [ ] **Step 7: Implement live-check claim, requeue, and final metadata persistence**

Update `core/db.py` so claim initializes:

```python
row["live_check_status"] = "queued"
row["live_check_ok"] = False
row["live_check_trigger"] = str(trigger or "manual")
row["live_check_backend"] = str(backend or "protocol")
row["live_check_attempt"] = 1
row["live_check_max_attempts"] = max(1, int(max_attempts or 1))
row["live_check_next_retry_at"] = None
row["live_check_failure_kind"] = None
```

Implement requeue with these exact state transitions:

```python
def requeue_account_live_check(
    acc_id: int,
    error: str,
    *,
    failure_kind: str,
    attempt: int,
    max_attempts: int,
    next_retry_at: str,
) -> bool:
    with _LOCK:
        rows = _load_accounts()
        row = next((r for r in rows if int(r.get("id") or 0) == int(acc_id)), None)
        if row is None or row.get("live_check_status") not in {"running", "queued"}:
            return False
        row["live_check_status"] = "queued"
        row["live_check_ok"] = False
        row["live_check_attempt"] = max(1, int(attempt))
        row["live_check_max_attempts"] = max(1, int(max_attempts))
        row["live_check_next_retry_at"] = str(next_retry_at or "") or None
        row["live_check_failure_kind"] = str(failure_kind or "unknown")
        row["live_check_error"] = str(error or "查活失败")[:500]
        row["updated_at"] = _now()
        _save_accounts(rows)
        return True
```

Extend `update_account_liveness()` to persist `backend`, `failure_kind`, `profile_id`, `profile_source`, and masked `proxy_used`, and always clear `live_check_next_retry_at` on terminal results. Keep all token writes inside the existing `if ok:` block.

- [ ] **Step 8: Run database and account-state regression tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_live_check_browser_service.py tests/test_account_list_query.py -q
```

Expected: PASS, including archived-account claim protection and dead-account classification.

- [ ] **Step 9: Commit Task 1**

```powershell
git add -p -- config/roxybrowser.py config/__init__.py webui/config_editor.py core/db.py tests/test_config_defaults.py tests/test_account_list_query.py
git add -- tests/test_live_check_browser_service.py
git commit -m "feat: persist browser live-check state"
```

---

### Task 2: Implement Session Identity Validation and Redaction Primitives

**Files:**
- Create: `core/roxy_live_check.py`
- Create: `tests/test_roxy_live_check.py`

**Interfaces:**
- Produces `RoxyLiveCheckFailure(failure_kind: str, message: str, *, retryable: bool, deactivated: bool = False)`.
- Produces `validate_browser_session(session_info: dict, account: dict, email: str, *, now_ts: float | None = None) -> dict`.
- Produces `safe_url_for_log(value: str) -> str` and `safe_error_text(value: object) -> str`.
- The returned validated session includes the original `accessToken`; callers must never log it.

- [ ] **Step 1: Write failing identity and redaction tests**

Create `tests/test_roxy_live_check.py`:

```python
import base64
import json
import time

import pytest


def _jwt(payload: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def _session(email="user@example.com", user_id="user-1", exp=None):
    expires = int(exp if exp is not None else time.time() + 3600)
    token = _jwt({
        "exp": expires,
        "https://api.openai.com/profile": {"email": email},
        "https://api.openai.com/auth": {"chatgpt_user_id": user_id},
    })
    return {
        "accessToken": token,
        "user": {"email": email, "id": user_id, "name": "User"},
        "account": {"planType": "free"},
        "expires": "2026-08-19T12:00:00.000Z",
    }


def test_validate_browser_session_accepts_matching_identity():
    from core.roxy_live_check import validate_browser_session

    result = validate_browser_session(
        _session(), {"email": "user@example.com", "user_id": "user-1"}, "USER@example.com"
    )
    assert result["user"]["id"] == "user-1"
    assert result["account"]["planType"] == "free"


@pytest.mark.parametrize(
    "session_info,account,email,kind",
    [
        ({"user": {"email": "user@example.com"}}, {}, "user@example.com", "session_missing"),
        (_session(email="other@example.com"), {}, "user@example.com", "profile_account_mismatch"),
        (_session(user_id="user-2"), {"user_id": "user-1"}, "user@example.com", "account_identity_mismatch"),
        (_session(exp=1), {}, "user@example.com", "session_expired"),
    ],
)
def test_validate_browser_session_rejects_unsafe_identity(session_info, account, email, kind):
    from core.roxy_live_check import RoxyLiveCheckFailure, validate_browser_session

    with pytest.raises(RoxyLiveCheckFailure) as caught:
        validate_browser_session(session_info, account, email, now_ts=1000)
    assert caught.value.failure_kind == kind


def test_safe_diagnostics_remove_callback_secrets_and_token_values():
    from core.roxy_live_check import safe_error_text, safe_url_for_log

    callback = "https://chatgpt.com/api/auth/callback/openai?code=secret-code&state=secret-state"
    assert safe_url_for_log(callback) == "https://chatgpt.com/api/auth/callback/openai"
    safe = safe_error_text(f"failed url={callback} accessToken=secret-token")
    assert "secret-code" not in safe
    assert "secret-state" not in safe
    assert "secret-token" not in safe
```

- [ ] **Step 2: Run tests and observe the missing module failure**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py -k "validate_browser_session or safe_diagnostics" -q
```

Expected: FAIL with `ModuleNotFoundError` or missing exported names.

- [ ] **Step 3: Implement typed failures, URL sanitization, and identity checks**

Create `core/roxy_live_check.py` with these public primitives:

```python
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit, urlunsplit

from core.chatgpt_plan import token_claims


class RoxyLiveCheckFailure(RuntimeError):
    def __init__(
        self,
        failure_kind: str,
        message: str,
        *,
        retryable: bool,
        deactivated: bool = False,
    ):
        super().__init__(message)
        self.failure_kind = str(failure_kind or "unknown")
        self.retryable = bool(retryable)
        self.deactivated = bool(deactivated)


def safe_url_for_log(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"


def safe_error_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s]+", lambda match: safe_url_for_log(match.group(0)), text)
    text = re.sub(r"(?i)(accessToken|authorization|cookie|code|state)=?[^\s,}]+", r"\1=<redacted>", text)
    return text[:500]


def validate_browser_session(
    session_info: dict,
    account: dict,
    email: str,
    *,
    now_ts: float | None = None,
) -> dict:
    session_info = session_info if isinstance(session_info, dict) else {}
    account = account if isinstance(account, dict) else {}
    token = str(session_info.get("accessToken") or "").strip()
    if not token:
        raise RoxyLiveCheckFailure("session_missing", "登录后未取得 accessToken", retryable=True)

    target_email = str(email or "").strip().lower()
    user = session_info.get("user") if isinstance(session_info.get("user"), dict) else {}
    session_email = str(user.get("email") or "").strip().lower()
    if not session_email or session_email != target_email:
        raise RoxyLiveCheckFailure(
            "profile_account_mismatch", "Roxy profile 登录邮箱与目标账号不一致", retryable=False
        )

    claims = token_claims(token)
    claim_email = str(claims.get("email") or "").strip().lower()
    if claim_email and claim_email != target_email:
        raise RoxyLiveCheckFailure(
            "profile_account_mismatch", "Token 邮箱与目标账号不一致", retryable=False
        )

    session_user_id = str(user.get("id") or "").strip()
    claim_user_id = str(claims.get("user_id") or "").strip()
    stored_user_id = str(account.get("user_id") or "").strip()
    if claim_user_id and session_user_id and claim_user_id != session_user_id:
        raise RoxyLiveCheckFailure(
            "account_identity_mismatch", "Token user_id 与 session 不一致", retryable=False
        )
    if stored_user_id and session_user_id and stored_user_id != session_user_id:
        raise RoxyLiveCheckFailure(
            "account_identity_mismatch", "Session user_id 与账号记录不一致", retryable=False
        )

    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and float(exp) <= float(now_ts if now_ts is not None else time.time()):
        raise RoxyLiveCheckFailure("session_expired", "新取得的 accessToken 已过期", retryable=True)
    return session_info
```

- [ ] **Step 4: Run focused tests and then the existing token-claim tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py tests/test_chatgpt_plan.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- core/roxy_live_check.py tests/test_roxy_live_check.py
git commit -m "feat: validate browser live-check identity"
```

---

### Task 3: Extract Existing-Account OTP Login from the Roxy Registration Flow

**Files:**
- Modify: `core/roxy_registration.py:1070-1317,1928-2073,2236-2325`
- Modify: `tests/test_roxy_live_check.py`
- Test: `tests/test_roxy_registration_otp_retry.py`
- Test: `tests/test_roxy_password_setup.py`

**Interfaces:**
- Produces `RoxyExistingLoginError(failure_kind: str, message: str, *, retryable: bool)` in `core.roxy_registration`.
- Produces `login_existing_account_with_otp(driver, email: str, *, progress_callback=None) -> dict`.
- Reuses `_safe_get`, `_maybe_accept`, `_submit_email_and_wait_next`, `_click_passwordless_signup_if_present`, `_is_email_verification_page`, `_type_otp`, `_wait_after_email_otp_submit`, `_click_resend_email_otp`, and `_fetch_chatgpt_session`.
- Must not call `_fill_password_page_if_present`, `_complete_profile_page`, or `_run_roxy_password_setup`.

- [ ] **Step 1: Write failing tests for existing-session and OTP paths**

Append to `tests/test_roxy_live_check.py`:

```python
from unittest.mock import Mock, patch


def test_existing_account_login_returns_current_session_without_otp():
    from core.roxy_registration import login_existing_account_with_otp

    driver = Mock(current_url="https://chatgpt.com/")
    session = {"accessToken": "token", "user": {"email": "user@example.com"}}
    with patch("core.roxy_registration._safe_get"), patch(
        "core.roxy_registration._read_chatgpt_session_once", return_value=session
    ), patch("core.roxy_registration.capture_otp_baseline") as baseline:
        result = login_existing_account_with_otp(driver, "user@example.com")

    assert result is session
    baseline.assert_not_called()


def test_existing_account_login_uses_otp_without_registration_or_password_actions():
    from core.roxy_registration import login_existing_account_with_otp

    driver = Mock(current_url="https://auth.openai.com/email-verification")
    session = {"accessToken": "token", "user": {"email": "user@example.com"}}
    forbidden = RuntimeError("forbidden helper called")
    with patch("core.roxy_registration._safe_get"), patch(
        "core.roxy_registration._read_chatgpt_session_once", return_value=None
    ), patch("core.roxy_registration.capture_otp_baseline", return_value={"message_ids": []}), patch(
        "core.roxy_registration._submit_email_and_wait_next", return_value="otp"
    ), patch("core.roxy_registration.wait_for_otp", return_value="123456"), patch(
        "core.roxy_registration._clear_otp_inputs"
    ), patch("core.roxy_registration._type_otp") as type_otp, patch(
        "core.roxy_registration._click_continue"
    ), patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"), patch(
        "core.roxy_registration._fetch_chatgpt_session", return_value=session
    ), patch("core.roxy_registration._fill_password_page_if_present", side_effect=forbidden), patch(
        "core.roxy_registration._complete_profile_page", side_effect=forbidden
    ), patch("core.roxy_registration._run_roxy_password_setup", side_effect=forbidden):
        result = login_existing_account_with_otp(driver, "user@example.com")

    assert result is session
    type_otp.assert_called_once_with(driver, "123456")


def test_existing_account_login_rejects_about_you_instead_of_completing_profile():
    from core.roxy_registration import RoxyExistingLoginError, login_existing_account_with_otp

    driver = Mock(current_url="https://auth.openai.com/about-you")
    with patch("core.roxy_registration._safe_get"), patch(
        "core.roxy_registration._read_chatgpt_session_once", return_value=None
    ), patch("core.roxy_registration.capture_otp_baseline", return_value={}), patch(
        "core.roxy_registration._submit_email_and_wait_next", return_value="otp"
    ), patch("core.roxy_registration.wait_for_otp", return_value="123456"), patch(
        "core.roxy_registration._clear_otp_inputs"
    ), patch("core.roxy_registration._type_otp"), patch(
        "core.roxy_registration._click_continue"
    ), patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"):
        with pytest.raises(RoxyExistingLoginError) as caught:
            login_existing_account_with_otp(driver, "user@example.com")

    assert caught.value.failure_kind == "account_incomplete"
    assert caught.value.retryable is False
```

- [ ] **Step 2: Run tests and observe the missing login function**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py -k "existing_account_login" -q
```

Expected: FAIL because `login_existing_account_with_otp` and `RoxyExistingLoginError` do not exist.

- [ ] **Step 3: Implement the existing-account login boundary**

Add to `core/roxy_registration.py` near the session helpers:

```python
class RoxyExistingLoginError(RuntimeError):
    def __init__(self, failure_kind: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.failure_kind = str(failure_kind or "unknown")
        self.retryable = bool(retryable)


def _existing_login_is_incomplete(driver) -> bool:
    url = str(getattr(driver, "current_url", "") or "").lower()
    return any(marker in url for marker in (
        "about-you", "create-account/password", "signup/profile", "create-account/profile"
    ))
```

Implement `login_existing_account_with_otp` with this order:

```python
def login_existing_account_with_otp(driver, email: str, *, progress_callback=None) -> dict:
    progress = progress_callback or (lambda message: None)
    _safe_get(
        driver,
        "https://chatgpt.com/auth/login",
        timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
        attempts=2,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
    )
    _maybe_accept(driver)
    existing = _read_chatgpt_session_once(driver)
    if existing:
        progress("[浏览器查活] 已检测到现有 ChatGPT session")
        return existing

    otp_baseline = capture_otp_baseline(email)
    otp_after_ts = time.time()
    next_state = _submit_email_and_wait_next(driver, email, attempts=3)
    if next_state != "otp":
        clicked = _click_passwordless_signup_if_present(driver)
        if not clicked.get("ok"):
            raise RoxyExistingLoginError(
                "account_incomplete", "现有账号登录进入密码或注册页面，未找到 OTP 登录入口", retryable=False
            )

    for attempt in range(1, 4):
        if _existing_login_is_incomplete(driver):
            raise RoxyExistingLoginError(
                "account_incomplete", "账号登录进入未完成注册资料页", retryable=False
            )
        progress(f"[浏览器查活] 等待邮箱 OTP attempt={attempt}/3")
        try:
            code = wait_for_otp(email, after_ts=otp_after_ts, otp_baseline=otp_baseline)
        except Exception as exc:
            if attempt >= 3:
                raise RoxyExistingLoginError("otp_timeout", "等待邮箱验证码超时", retryable=True) from exc
            otp_baseline = capture_otp_baseline(email)
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            continue

        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        try:
            _click_continue(driver)
        except Exception:
            logger.info("%s 浏览器查活 OTP 页面没有显式提交按钮，继续观察页面状态", _log_prefix(driver))
        outcome = _wait_after_email_otp_submit(driver, timeout=12)
        if outcome == "accepted":
            break
        if attempt >= 3:
            raise RoxyExistingLoginError("otp_invalid", "邮箱验证码连续无效或过期", retryable=True)
        otp_baseline = capture_otp_baseline(email)
        otp_after_ts = time.time()
        _click_resend_email_otp(driver, timeout=25)

    if _existing_login_is_incomplete(driver):
        raise RoxyExistingLoginError("account_incomplete", "账号登录进入未完成注册资料页", retryable=False)
    progress("[浏览器查活] OTP 已验证，等待 ChatGPT session")
    return _fetch_chatgpt_session(driver, timeout=90)
```

Do not copy the registration log line that prints `current_otp`. Progress and logger messages may contain attempt numbers but never `code`.

- [ ] **Step 4: Run the login and existing Roxy regression tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py tests/test_roxy_registration_otp_retry.py tests/test_roxy_password_setup.py -q
```

Expected: PASS; password setup and registration still use their existing paths.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -p -- core/roxy_registration.py tests/test_roxy_live_check.py
git commit -m "feat: add Roxy existing-account OTP login"
```

---

### Task 4: Implement Roxy Profile Lifecycle and Browser Live-Check Orchestration

**Files:**
- Modify: `core/roxy_live_check.py`
- Modify: `tests/test_roxy_live_check.py`
- Test: `tests/test_roxy_saved_proxy.py`
- Test: `tests/test_roxy_window_position.py`

**Interfaces:**
- Produces `check_account_liveness_with_roxy(account_id: int, email: str, *, progress_callback=None) -> dict`.
- Consumes `login_existing_account_with_otp(driver, email: str, *, progress_callback=None) -> dict` and `validate_browser_session(session_info: dict, account: dict, email: str, *, now_ts: float | None = None) -> dict`.
- Result always contains `ok`, `status`, `backend`, `failure_kind`, `checked_at`, and `retryable`; success additionally contains `access_token`, `session`, `profile_id`, `profile_source`, and masked `proxy_used`.

- [ ] **Step 1: Write failing tests for saved profile, mismatch, recovery, and cleanup order**

Add focused fake objects to `tests/test_roxy_live_check.py`:

```python
from types import SimpleNamespace


class FakeDriver:
    def __init__(self):
        self.quit_called = False

    def quit(self):
        self.quit_called = True


class FakeRoxyClient:
    def __init__(self, driver, stale=False):
        self.driver = driver
        self.stale = stale
        self.created = 0
        self.opened = []
        self.closed = []
        self.deleted = []

    def create_profile(self):
        self.created += 1
        return "temporary-profile"

    def open_profile(self, profile_id, *, allow_existing_profile=False):
        self.opened.append((profile_id, allow_existing_profile))
        if profile_id == "saved-profile" and self.stale:
            raise RuntimeError("HTTP 404 profile not found")
        created = profile_id == "temporary-profile"
        return SimpleNamespace(
            profile_id=profile_id,
            created_by_run=created,
            raw={"proxyInfo": {"protocol": "SOCKS5", "host": "proxy.example", "port": "1080", "proxyPassword": "secret"}},
        )

    def close_profile(self, profile_id):
        assert self.driver.quit_called
        self.closed.append(profile_id)

    def delete_profile(self, profile_id):
        assert self.driver.quit_called
        self.deleted.append(profile_id)


def test_browser_live_check_reuses_saved_profile_and_refreshes_token():
    from core import roxy_live_check

    driver = FakeDriver()
    client = FakeRoxyClient(driver)
    account = {"id": 1, "email": "user@example.com", "user_id": "user-1", "extra_json": json.dumps({"roxybrowser": {"profile_id": "saved-profile"}})}
    session = _session()
    with patch("core.roxy_live_check.db.get_account", return_value=account), patch(
        "core.roxy_live_check.RoxyBrowserClient", return_value=client
    ), patch("core.roxy_live_check._build_driver", return_value=driver), patch(
        "core.roxy_live_check.login_existing_account_with_otp", return_value=session
    ):
        result = roxy_live_check.check_account_liveness_with_roxy(1, "user@example.com")

    assert result["ok"] is True
    assert result["status"] == "live"
    assert result["backend"] == "browser"
    assert result["profile_source"] == "saved"
    assert result["proxy_used"] == "socks5://proxy.example:1080"
    assert client.closed == ["saved-profile"]
    assert client.deleted == []


def test_stale_saved_profile_creates_one_temporary_profile_and_deletes_it():
    from core import roxy_live_check

    driver = FakeDriver()
    client = FakeRoxyClient(driver, stale=True)
    account = {"id": 1, "email": "user@example.com", "extra_json": json.dumps({"roxybrowser": {"profile_id": "saved-profile"}})}
    with patch("core.roxy_live_check.db.get_account", return_value=account), patch(
        "core.roxy_live_check.RoxyBrowserClient", return_value=client
    ), patch("core.roxy_live_check._build_driver", return_value=driver), patch(
        "core.roxy_live_check.login_existing_account_with_otp", return_value=_session()
    ), patch.object(roxy_live_check.roxy_cfg, "LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE", True):
        result = roxy_live_check.check_account_liveness_with_roxy(1, "user@example.com")

    assert result["ok"] is True
    assert client.created == 1
    assert client.deleted == ["temporary-profile"]


def test_browser_live_check_mismatch_is_terminal_and_does_not_return_token():
    from core import roxy_live_check

    driver = FakeDriver()
    client = FakeRoxyClient(driver)
    account = {"id": 1, "email": "user@example.com", "extra_json": "{}"}
    with patch("core.roxy_live_check.db.get_account", return_value=account), patch(
        "core.roxy_live_check.RoxyBrowserClient", return_value=client
    ), patch("core.roxy_live_check._build_driver", return_value=driver), patch(
        "core.roxy_live_check.login_existing_account_with_otp", return_value=_session(email="other@example.com")
    ):
        result = roxy_live_check.check_account_liveness_with_roxy(1, "user@example.com")

    assert result["ok"] is False
    assert result["failure_kind"] == "profile_account_mismatch"
    assert result["retryable"] is False
    assert "access_token" not in result


def test_browser_live_check_progress_never_contains_session_secrets():
    from core import roxy_live_check

    messages = []
    driver = FakeDriver()
    client = FakeRoxyClient(driver)
    account = {"id": 1, "email": "user@example.com", "extra_json": "{}"}
    sensitive_session = _session()
    sensitive_session["callback"] = (
        "https://chatgpt.com/api/auth/callback/openai?code=secret-code&state=secret-state"
    )
    with patch("core.roxy_live_check.db.get_account", return_value=account), patch(
        "core.roxy_live_check.RoxyBrowserClient", return_value=client
    ), patch("core.roxy_live_check._build_driver", return_value=driver), patch(
        "core.roxy_live_check.login_existing_account_with_otp", return_value=sensitive_session
    ):
        result = roxy_live_check.check_account_liveness_with_roxy(
            1, "user@example.com", progress_callback=messages.append
        )

    rendered = "\n".join(messages)
    assert result["ok"] is True
    assert sensitive_session["accessToken"] not in rendered
    assert "secret-code" not in rendered
    assert "secret-state" not in rendered
```

- [ ] **Step 2: Run orchestration tests and observe the missing function**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py -k "browser_live_check or stale_saved_profile" -q
```

Expected: FAIL because `check_account_liveness_with_roxy` is not implemented.

- [ ] **Step 3: Implement profile parsing, stale recovery, masked proxy extraction, and cleanup**

Add private helpers to `core/roxy_live_check.py`:

```python
import json
from datetime import datetime

from config import roxybrowser as roxy_cfg
from core import db
from core.openai_auth import detect_account_unusable_text
from core.roxy_registration import (
    RoxyExistingLoginError,
    _build_driver,
    login_existing_account_with_otp,
)
from core.roxybrowser_client import RoxyBrowserClient


def _profile_id(account: dict) -> str:
    raw = account.get("extra_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    roxy = raw.get("roxybrowser") if isinstance(raw, dict) else {}
    return str((roxy or {}).get("profile_id") or "").strip()


def _is_stale_profile_error(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "http 404", "http 502", "http 503", "profile not found", "dirid", "数据不存在", "窗口/数据不存在"
    ))


def _masked_proxy(opened) -> str | None:
    raw = getattr(opened, "raw", {}) if opened is not None else {}
    info = raw.get("proxyInfo") if isinstance(raw, dict) else {}
    info = info if isinstance(info, dict) else {}
    host = str(info.get("host") or "").strip()
    port = str(info.get("port") or "").strip()
    protocol = str(info.get("protocol") or info.get("proxyCategory") or "").strip().lower()
    if not host:
        return None
    scheme = "socks5" if protocol == "socks5" else protocol or "http"
    return f"{scheme}://{host}{':' + port if port else ''}"


def _open_for_live_check(client, saved_profile_id: str, progress):
    if not saved_profile_id:
        temporary_id = client.create_profile()
        return client.open_profile(temporary_id, allow_existing_profile=True), "temporary"
    try:
        return client.open_profile(saved_profile_id, allow_existing_profile=True), "saved"
    except Exception as exc:
        if not _is_stale_profile_error(exc):
            raise RoxyLiveCheckFailure(
                "browser_open_failed", safe_error_text(exc), retryable=True
            ) from exc
        progress("[浏览器查活] 历史 profile 已失效，创建一个临时环境")
        temporary_id = client.create_profile()
        return client.open_profile(temporary_id, allow_existing_profile=True), "temporary"
```

Cleanup must call `driver.quit()` first, then `client.close_profile(profile_id)`, and call `client.delete_profile(profile_id)` only when `profile_source == "temporary"`, `LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE=True`, and `ROXY_KEEP_BROWSER_OPEN=False`.

- [ ] **Step 4: Implement the public browser live-check result boundary**

Use one terminal-result function so every result has consistent keys:

```python
def _failed_result(kind: str, error: object, *, retryable: bool, deactivated: bool = False) -> dict:
    return {
        "ok": False,
        "status": "deactivated" if deactivated else "failed",
        "backend": "browser",
        "failure_kind": kind,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "retryable": bool(retryable),
        "error": safe_error_text(error),
    }
```

`check_account_liveness_with_roxy()` must:

1. Load the account by `account_id` and reject missing/mismatched email.
2. Open saved or one temporary profile.
3. Build the driver.
4. Call `login_existing_account_with_otp`.
5. Call `validate_browser_session`.
6. Return success with token/session/profile diagnostics.
7. Translate `RoxyExistingLoginError` and `RoxyLiveCheckFailure` without changing their retryability.
8. Use `detect_account_unusable_text` only to classify explicit account-unusable text as `deactivated`.
9. Translate Selenium/network/profile exceptions to `browser_open_failed`, `network_unavailable`, or `unknown`; never infer deactivation from HTTP 403.
10. Execute cleanup in `finally`; cleanup errors are logged through `safe_error_text` and never replace a successful result.

- [ ] **Step 5: Run profile lifecycle and proxy regression tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py tests/test_roxy_saved_proxy.py tests/test_roxy_window_position.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```powershell
git add -- core/roxy_live_check.py tests/test_roxy_live_check.py
git commit -m "feat: orchestrate Roxy browser live check"
```

---

### Task 5: Add Mode Dispatch, Browser Queue, and Delayed Retry

**Files:**
- Modify: `core/live_check_service.py:1-144`
- Modify: `tests/test_live_check_browser_service.py`
- Test: `tests/test_account_list_query.py`

**Interfaces:**
- Produces `normalize_live_check_mode(value: str | None) -> str`.
- Changes `enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None, mode: str = "protocol") -> dict`.
- Produces `_run_browser_live_check(*, account_id: int, email: str, trigger: str) -> dict` and `_run_browser_task_wrapper(*, account_id: int, email: str, trigger: str) -> dict`.
- Changes `queue_settings(mode: str = "protocol") -> dict` while preserving the existing flat protocol response shape.

- [ ] **Step 1: Write failing dispatch and queue ownership tests**

Add to `tests/test_live_check_browser_service.py`:

```python
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


def test_normalize_live_check_mode_accepts_two_modes_and_defaults_protocol():
    from core.live_check_service import normalize_live_check_mode

    assert normalize_live_check_mode(None) == "protocol"
    assert normalize_live_check_mode("protocol") == "protocol"
    assert normalize_live_check_mode("browser") == "browser"
    with pytest.raises(ValueError):
        normalize_live_check_mode("auto")


def test_browser_mode_dispatches_only_to_roxy_backend():
    from core import live_check_service as service

    slots = Mock()
    slots.acquire.return_value = True
    executor = Mock()
    executor.submit.return_value = SimpleNamespace()
    with patch.object(service, "_BROWSER_QUEUE_SLOTS", slots), patch.object(
        service, "_BROWSER_EXECUTOR", executor
    ), patch.object(service.db, "claim_account_live_check", return_value=True), patch.object(
        service, "_append_log"
    ):
        result = service.enqueue_account_live_check(
            account_id=1, email="user@example.com", mode="browser"
        )

    assert result["accepted"] is True
    assert result["mode"] == "browser"
    executor.submit.assert_called_once()
    assert executor.submit.call_args.kwargs["account_id"] == 1


def test_protocol_and_browser_modes_share_atomic_account_claim():
    from core import live_check_service as service

    slots = Mock()
    slots.acquire.return_value = True
    with patch.object(service, "_BROWSER_QUEUE_SLOTS", slots), patch.object(
        service.db, "claim_account_live_check", return_value=False
    ), patch.object(service, "_append_log"):
        result = service.enqueue_account_live_check(
            account_id=1, email="user@example.com", mode="browser"
        )

    assert result["accepted"] is False
    assert result["busy"] is True
    slots.release.assert_called_once_with()
```

- [ ] **Step 2: Run tests and observe missing mode/browser executor failures**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_live_check_browser_service.py -k "mode or dispatches or atomic" -q
```

Expected: FAIL because mode normalization and browser queue objects do not exist.

- [ ] **Step 3: Add mode normalization and separate browser resources**

At module load, read bounded settings from `config.roxybrowser`:

```python
from config import roxybrowser as roxy_cfg
from core.roxy_live_check import check_account_liveness_with_roxy


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(roxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_live_check_mode(value: str | None) -> str:
    mode = str(value or "protocol").strip().lower()
    if mode not in {"protocol", "browser"}:
        raise ValueError("查活方式只支持 protocol 或 browser")
    return mode


_BROWSER_WORKERS = _bounded_int("LIVE_CHECK_BROWSER_WORKERS", 1, 1, 4)
_BROWSER_QUEUE_LIMIT = _bounded_int(
    "LIVE_CHECK_BROWSER_QUEUE_LIMIT", 100, _BROWSER_WORKERS, 500
)
_BROWSER_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BROWSER_WORKERS, thread_name_prefix="browser-live-check"
)
_BROWSER_QUEUE_SLOTS = threading.BoundedSemaphore(_BROWSER_QUEUE_LIMIT)
```

In `enqueue_account_live_check`, normalize mode before acquiring a slot, choose the protocol or browser semaphore/executor, and call `db.claim_account_live_check` with `backend=mode` and browser max attempts. The returned object and initial log line must include `mode`.

- [ ] **Step 4: Write failing delayed-retry tests**

Add:

```python
def test_browser_retry_releases_worker_before_arming_timer():
    from core import live_check_service as service

    events = []
    with patch.object(
        service,
        "_run_browser_live_check",
        return_value={
            "ok": False,
            "status": "failed",
            "backend": "browser",
            "failure_kind": "network_unavailable",
            "retryable": True,
            "error": "page load timeout",
            "attempt": 1,
            "max_attempts": 3,
        },
    ), patch.object(
        service._BROWSER_QUEUE_SLOTS, "release", side_effect=lambda: events.append("release")
    ), patch.object(
        service, "_schedule_browser_retry", side_effect=lambda **kwargs: events.append("retry") or True
    ):
        result = service._run_browser_task_wrapper(
            account_id=1, email="user@example.com", trigger="manual"
        )

    assert result["ok"] is False
    assert events == ["release", "retry"]


def test_non_retryable_browser_failure_is_written_once_without_timer():
    from core import live_check_service as service

    result = {
        "ok": False,
        "status": "failed",
        "backend": "browser",
        "failure_kind": "profile_account_mismatch",
        "retryable": False,
        "error": "profile mismatch",
        "attempt": 1,
        "max_attempts": 3,
    }
    with patch.object(service, "_run_browser_live_check", return_value=result), patch.object(
        service.db, "update_account_liveness"
    ) as update, patch.object(service, "_schedule_browser_retry") as retry, patch.object(
        service._BROWSER_QUEUE_SLOTS, "release"
    ):
        service._run_browser_task_wrapper(
            account_id=1, email="user@example.com", trigger="manual"
        )

    retry.assert_not_called()
    update.assert_called_once_with(1, result)
```

- [ ] **Step 5: Run retry tests and observe missing wrapper/scheduler failures**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_live_check_browser_service.py -k "retry or non_retryable" -q
```

Expected: FAIL because browser wrapper and scheduler do not exist.

- [ ] **Step 6: Implement browser execution and delayed retry**

Implement `_run_browser_live_check` to mark running, read current attempt/max attempts from `db.get_account`, call `check_account_liveness_with_roxy`, add attempt metadata, and append `[浏览器查活]` progress lines.

Parse retry delays exactly:

```python
def _browser_retry_delays() -> list[int]:
    values = []
    for raw in str(getattr(roxy_cfg, "LIVE_CHECK_BROWSER_RETRY_DELAYS", "15,60,180") or "").split(","):
        try:
            values.append(max(0, min(3600, int(raw.strip()))))
        except (TypeError, ValueError):
            continue
    return values or [15, 60, 180]


def _browser_retry_delay(attempt: int) -> int:
    values = _browser_retry_delays()
    return values[min(max(1, int(attempt)) - 1, len(values) - 1)]
```

`_run_browser_task_wrapper` must release `_BROWSER_QUEUE_SLOTS` in `finally` before calling `_schedule_browser_retry`. The scheduler must:

1. Return `False` when `retryable` is false or `attempt >= max_attempts`.
2. Persist queued state through `db.requeue_account_live_check`.
3. Use a daemon `threading.Timer`.
4. Acquire a browser queue slot only when the timer fires.
5. Re-arm for five seconds if the queue is full.
6. Submit `_run_browser_task_wrapper` without calling `claim_account_live_check` again.
7. Mark a terminal failure if timer creation or executor submission fails.

- [ ] **Step 7: Preserve protocol execution and expose mode-specific queue settings**

Keep the existing protocol `_run_live_check` behavior and executor intact. Implement:

```python
def queue_settings(mode: str = "protocol") -> dict:
    normalized = normalize_live_check_mode(mode)
    if normalized == "browser":
        return {
            "backend": "browser",
            "workers": _BROWSER_WORKERS,
            "queue_limit": _BROWSER_QUEUE_LIMIT,
            "max_attempts": _bounded_int("LIVE_CHECK_BROWSER_MAX_ATTEMPTS", 3, 1, 10),
            "retry_delays": _browser_retry_delays(),
        }
    return {"backend": "protocol", "workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
```

- [ ] **Step 8: Run service and account-state regression tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_live_check_browser_service.py tests/test_account_list_query.py tests/test_codex_dead_account_detection.py -q
```

Expected: PASS; browser infrastructure failures remain `failed`, never `deactivated`.

- [ ] **Step 9: Commit Task 5**

```powershell
git add -p -- core/live_check_service.py tests/test_live_check_browser_service.py
git commit -m "feat: queue browser live-check tasks"
```

---

### Task 6: Add Browser Live Check to API and Account UI

**Files:**
- Modify: `webui/app.py:160-175,800-869`
- Modify: `webui/templates/index.html:2090-2120,3450-3535,4572-4611`
- Modify: `tests/test_webui_account_features.py`

**Interfaces:**
- `POST /api/accounts/check-live-bulk` consumes `{account_ids: list[int], mode?: "protocol" | "browser"}`.
- Invalid mode returns HTTP 400 before account lookup or queue mutation.
- API response includes top-level `mode`, each started item's `mode`, and mode-specific `queue` settings.
- Frontend calls `checkSelectedLive(idsArg=null, btnArg=null, mode="protocol")`.

- [ ] **Step 1: Write failing API tests**

Add to `tests/test_webui_account_features.py`:

```python
    @patch("webui.app.live_check_service.enqueue_account_live_check")
    @patch("webui.app.live_check_service.queue_settings")
    @patch("webui.app.db.get_account")
    def test_browser_live_check_bulk_forwards_explicit_mode(self, get_account, queue_settings, enqueue):
        get_account.return_value = {"id": 7, "email": "user@example.com"}
        queue_settings.return_value = {"backend": "browser", "workers": 1, "queue_limit": 100}
        enqueue.return_value = {
            "accepted": True,
            "account_id": 7,
            "email": "user@example.com",
            "status": "queued",
            "mode": "browser",
        }

        response = self.client.post(
            "/api/accounts/check-live-bulk",
            json={"account_ids": [7], "mode": "browser"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "browser")
        self.assertEqual(payload["started"][0]["mode"], "browser")
        enqueue.assert_called_once_with(
            account_id=7,
            email="user@example.com",
            trigger="manual",
            proxy=None,
            mode="browser",
        )
        queue_settings.assert_called_once_with("browser")

    @patch("webui.app.live_check_service.enqueue_account_live_check")
    def test_live_check_bulk_rejects_auto_mode_before_enqueue(self, enqueue):
        response = self.client.post(
            "/api/accounts/check-live-bulk",
            json={"account_ids": [7], "mode": "auto"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("protocol 或 browser", response.get_json()["error"])
        enqueue.assert_not_called()

    @patch("webui.app.live_check_service.enqueue_account_live_check")
    @patch("webui.app.live_check_service.queue_settings")
    @patch("webui.app.db.get_account")
    def test_live_check_bulk_defaults_to_protocol(self, get_account, queue_settings, enqueue):
        get_account.return_value = {"id": 7, "email": "user@example.com"}
        queue_settings.return_value = {"backend": "protocol", "workers": 3, "queue_limit": 500}
        enqueue.return_value = {"accepted": True, "mode": "protocol"}
        response = self.client.post("/api/accounts/check-live-bulk", json={"account_ids": [7]})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["mode"], "protocol")
```

- [ ] **Step 2: Run API tests and observe missing mode behavior**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py -k "live_check_bulk" -q
```

Expected: FAIL because the route neither validates nor forwards `mode`.

- [ ] **Step 3: Implement API mode validation and forwarding**

At the start of `api_accounts_check_live_bulk()`:

```python
        try:
            mode = live_check_service.normalize_live_check_mode(data.get("mode"))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
```

Pass `mode=mode` to each enqueue call, include mode in started items and top-level response, and call `live_check_service.queue_settings(mode)`.

Extend `_compact_account_for_list`'s allowlist with:

```python
"live_check_backend", "live_check_failure_kind", "live_check_attempt",
"live_check_max_attempts", "live_check_next_retry_at", "live_check_profile_source",
```

Do not expose `live_check_profile_id` by default; the log already provides it and the compact list should avoid unnecessary environment identifiers.

- [ ] **Step 4: Run API tests and confirm they pass**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py -k "live_check_bulk" -q
```

Expected: PASS.

- [ ] **Step 5: Write failing template tests for explicit actions and status labels**

Add:

```python
    def test_account_template_exposes_protocol_and_browser_live_check_actions(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("协议查活", html)
        self.assertIn("浏览器查活", html)
        self.assertIn("checkSelectedLive", html)
        self.assertIn("'browser'", html)
        self.assertIn("live_check_backend", html)
        self.assertIn("live_check_failure_kind", html)
        self.assertIn("浏览器查活默认单并发", html)
```

- [ ] **Step 6: Run the template test and observe missing browser controls**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py -k "protocol_and_browser_live_check" -q
```

Expected: FAIL because the page has only the generic protocol check action.

- [ ] **Step 7: Implement explicit protocol/browser controls and status rendering**

Change the JavaScript signature and request body:

```javascript
async function checkSelectedLive(idsArg = null, btnArg = null, mode = 'protocol') {
  const normalizedMode = mode === 'browser' ? 'browser' : 'protocol';
  const ids = idsArg || Array.from(ACCOUNT_SELECTED);
  if (ids.length === 0) { showToast('请先选择账号'); return; }
  const modeLabel = normalizedMode === 'browser' ? '浏览器查活' : '协议查活';
  const detail = normalizedMode === 'browser'
    ? '会通过 Roxy 真实浏览器登录邮箱 OTP；浏览器查活默认单并发。'
    : '会通过协议会话重新登录邮箱 OTP。';
  if (!confirm(`确定对 ${ids.length} 个账号执行${modeLabel}并刷新 AT/accessToken 吗？\n\n${detail}`)) return;
  const response = await api('/api/accounts/check-live-bulk', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({account_ids: ids, mode: normalizedMode}),
  });
  showToast(`${modeLabel}已入队 ${response.started_count || 0} 个`);
}
```

Keep error/finally behavior from the existing function around this focused change. Add two toolbar buttons and two row actions that pass `'protocol'` or `'browser'`. Render backend labels as “协议”/“浏览器”; map failure kinds to concise Chinese labels without treating `failed` as “已废”.

- [ ] **Step 8: Run WebUI account tests**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_webui_account_features.py tests/test_webui_jobs.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 6**

```powershell
git add -p -- webui/app.py webui/templates/index.html tests/test_webui_account_features.py
git commit -m "feat: expose Roxy browser live check"
```

---

### Task 7: Security Regression, Operator Documentation, and Full Verification

**Files:**
- Modify: `README.md`
- Verify: all files changed in Tasks 1-6

**Interfaces:**
- No new runtime interface.
- Delivers operator instructions and the complete security/regression gate.

- [ ] **Step 1: Document browser live-check operation**

Add a concise README section containing these exact operational points:

```markdown
### Roxy 浏览器查活

账号页提供“协议查活”和“浏览器查活”。协议查活仍是默认方式；浏览器查活会打开账号保存的
Roxy profile，或在 profile 不存在时创建一个临时环境，通过邮箱 OTP 登录后刷新
`accessToken`。

- 浏览器查活默认单并发，可在配置页调整队列容量、最大尝试次数和退避秒数。
- 历史 profile 只关闭不删除；任务创建的临时 profile 才受“删除查活临时环境”配置控制。
- profile 已登录其他邮箱时任务失败且不会写回 token，也不会自动退出或清理历史 profile。
- 浏览器、网络、OTP 和 session 错误不会被判为废号；只有明确停用/删除/封禁信号会标记已废。
- 查活失败保留旧 token。浏览器查活不能保证绕过第三方风控，也不会自动切换查活后端或网络出口。
```

- [ ] **Step 2: Run focused feature suites**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest tests/test_roxy_live_check.py tests/test_live_check_browser_service.py tests/test_account_list_query.py tests/test_config_defaults.py tests/test_webui_account_features.py tests/test_roxy_registration_otp_retry.py tests/test_roxy_password_setup.py tests/test_roxy_saved_proxy.py tests/test_roxy_window_position.py -q
```

Expected: PASS with no warnings caused by browser live check.

- [ ] **Step 3: Run the full regression suite**

Run:

```powershell
& '.venv\Scripts\python.exe' -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Inspect the final diff and sensitive-string surface**

Run:

```powershell
git diff --check
git diff --stat
rg -n "收到验证码：|accessToken.*%s|callback.*code=|Cookie.*value" core/roxy_live_check.py core/roxy_registration.py core/live_check_service.py
```

Expected: `git diff --check` is clean. Any `rg` result must be outside the new browser live-check call path or replaced with redacted logging before completion.

- [ ] **Step 5: Perform bounded manual acceptance**

Use one account at a time with `LIVE_CHECK_BROWSER_WORKERS=1`:

1. Run browser live check on a saved profile already logged into the matching account; verify no OTP is requested and token refresh succeeds.
2. Run browser live check on an account without a profile; verify one temporary profile is created, OTP login succeeds, and the profile follows cleanup configuration.
3. Use a test profile logged into a different account; verify `profile_account_mismatch`, no token overwrite, and no profile deletion.
4. Force an OTP timeout; verify `failed/otp_timeout`, old token preservation, and no `deactivated` state.
5. Open the per-account log and verify it contains no OTP, token, Cookie, callback code/state, proxy credentials, or password.

- [ ] **Step 6: Commit Task 7**

```powershell
git add -p -- README.md
git commit -m "docs: verify browser live-check safety"
```

---

## Completion Checklist

- [ ] `protocol` remains the API and UI default.
- [ ] `browser` is explicit and never selected automatically.
- [ ] Matching saved sessions refresh token without OTP.
- [ ] Missing sessions use existing-account email OTP login only.
- [ ] Password setup, registration password pages, and profile completion are unreachable from browser live check.
- [ ] Email/JWT/user-id/expiration validation blocks cross-account token writes.
- [ ] Historical profiles are never deleted; temporary profiles obey the dedicated setting.
- [ ] Browser worker defaults to one and delayed retry does not occupy a worker.
- [ ] Only `account_unusable` produces `deactivated`.
- [ ] Failed checks preserve the previous token and unrelated account state.
- [ ] Logs/API responses contain no OTP, token, Cookie, callback secrets, proxy credentials, or passwords.
- [ ] Focused tests and full `pytest -q` pass.
