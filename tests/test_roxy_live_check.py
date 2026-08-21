# -*- coding: utf-8 -*-
import base64
import json
import time
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    assert safe_url_for_log("http://proxy-user:proxy-pass@example.com:1080") == "http://example.com:1080"
    safe = safe_error_text(f"failed url={callback} accessToken=secret-token")
    assert "secret-code" not in safe
    assert "secret-state" not in safe
    assert "secret-token" not in safe


def test_safe_diagnostics_remove_proxy_credentials():
    from core.roxy_live_check import safe_error_text

    safe = safe_error_text("proxyUserName=proxy-user proxyPassword=proxy-pass password=account-pass")
    assert "proxy-user" not in safe
    assert "proxy-pass" not in safe
    assert "account-pass" not in safe
    json_safe = safe_error_text('{"proxyPassword": "json-proxy-pass", "accessToken": "json-token"}')
    assert "json-proxy-pass" not in json_safe
    assert "json-token" not in json_safe


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


def test_session_probe_progress_reports_http_403_without_secret_text():
    from core import roxy_registration

    driver = Mock()
    driver.execute_async_script.return_value = {
        "ok": False,
        "http_status": 403,
        "content_type": "text/html",
        "title": "Cloudflare",
        "summary": "Cloudflare challenge accessToken=secret-token",
    }
    messages = []

    result = roxy_registration._read_chatgpt_session_once(
        driver, progress_callback=messages.append,
    )

    assert result is None
    rendered = "\n".join(messages)
    assert "phase=session_probe" in rendered
    assert "http_status=403" in rendered
    assert "secret-token" not in rendered


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
    assert "phase=profile_open" in rendered
    assert "phase=driver_start" in rendered
    assert "phase=session_validate" in rendered
    assert "phase=cleanup" in rendered
    assert "saved-profile" not in rendered
