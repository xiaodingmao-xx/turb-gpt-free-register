# -*- coding: utf-8 -*-
import unittest
from urllib.parse import parse_qs, parse_qsl, urlparse
from unittest.mock import patch

from core.roxy_registration import (
    PasswordAlreadySetError,
    _build_password_setup_request,
    _fetch_password_setup_authorize_url,
    _password_already_set_in_text,
    _normalize_password_setup_mode,
    _type_otp,
)


class RoxyPasswordSetupTests(unittest.TestCase):
    def test_password_setup_failure_still_saves_registration_as_success(self):
        from core import roxy_registration as service
        from core.roxybrowser_client import RoxyOpenResult

        class FakeDriver:
            def set_page_load_timeout(self, _timeout):
                pass

            def set_script_timeout(self, _timeout):
                pass

            def quit(self):
                pass

        class FakeClient:
            def open_profile(self):
                return RoxyOpenResult("profile-1", {"code": 0})

            def cleanup_profile(self, _opened):
                pass

        with patch.object(service, "RoxyBrowserClient", return_value=FakeClient()), patch.object(
            service, "_build_driver", return_value=FakeDriver()
        ), patch.object(service, "_center_browser_window"), patch.object(service, "_safe_get"), patch.object(
            service, "_submit_email_and_wait_next", return_value="otp"
        ), patch.object(service, "_complete_profile_page", return_value=True), patch.object(
            service,
            "_fetch_chatgpt_session",
            return_value={"accessToken": "access-token", "user": {}, "account": {}, "expires": None},
        ), patch.object(service, "_type_otp"), patch.object(service, "_click_continue"), patch.object(
            service, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(
            service,
            "_run_password_setup_with_gate",
            side_effect=RuntimeError("密码设置获取 CSRF 失败"),
        ), patch.object(service, "detect_selenium_exit_ip", return_value="203.0.113.9"), patch.object(
            service, "save_account_data", return_value=42
        ) as save_account, patch.object(
            service, "resolve_email_source", return_value="generic_api"
        ), patch.object(service, "_check_manual_stop"), patch.object(
            service, "human_delay"
        ), patch.object(service._cfg, "ROXY_PASSWORD_SETUP_ENABLED", True), patch.object(
            service._cfg, "ROXY_KEEP_BROWSER_OPEN", False
        ), patch.object(service._twofa_cfg, "ENABLE_2FA", False), patch(
            "config.codex.ENABLE_CODEX_AUTO", False
        ):
            result = service.run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="2000-01-01",
                otp_code="123456",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], 42)
        self.assertTrue(result["password_setup_handoff"])
        save_account.assert_called_once()
        extra = save_account.call_args.kwargs["extra"]
        self.assertEqual(extra["password_setup_status"], "failed")
        self.assertIn("密码设置获取 CSRF 失败", extra["password_setup_error"])

    def test_password_already_set_error_is_detected_in_english_and_japanese(self):
        self.assertTrue(_password_already_set_in_text("error_code: password_already_set"))
        self.assertTrue(_password_already_set_in_text("パスワードはすでに設定済みです。"))
        self.assertFalse(_password_already_set_in_text("password updated"))
        self.assertTrue(issubclass(PasswordAlreadySetError, RuntimeError))

    def test_password_setup_stops_otp_retry_when_page_already_advanced(self):
        from unittest.mock import patch
        from core.roxy_registration import _run_roxy_password_setup

        driver = object()
        with patch("core.roxy_registration._fetch_password_setup_authorize_url", return_value="https://auth.openai.com/email-verification"), patch(
            "core.roxy_registration._safe_get"
        ), patch(
            "core.roxy_registration._is_email_verification_page", side_effect=[True, False]
        ), patch(
            "core.roxy_registration.capture_otp_baseline", return_value=None
        ), patch(
            "core.roxy_registration.wait_for_otp", return_value="123456"
        ), patch("core.roxy_registration._type_otp") as type_otp, patch(
            "core.roxy_registration._fill_password_setup_page"
        ) as fill_password:
            result = _run_roxy_password_setup(
                driver,
                "user@example.com",
                password="valid-password-123",
            )

        self.assertEqual(result, "valid-password-123")
        type_otp.assert_not_called()
        fill_password.assert_called_once()

    def test_password_setup_first_attempt_does_not_resend_or_exclude_previous_otp(self):
        from core.roxy_registration import _run_roxy_password_setup

        driver = object()
        with patch("core.roxy_registration._fetch_password_setup_authorize_url", return_value="https://auth.openai.com/email-verification"), patch(
            "core.roxy_registration._safe_get"
        ), patch(
            "core.roxy_registration._is_email_verification_page", return_value=True
        ), patch(
            "core.roxy_registration._click_resend_email_otp"
        ) as resend, patch(
            "core.roxy_registration.resolve_email_source", return_value="outlook"
        ), patch(
            "core.roxy_registration.capture_otp_baseline", return_value=None
        ), patch(
            "core.roxy_registration.wait_for_otp", return_value="222222"
        ) as wait_otp, patch("core.roxy_registration._type_otp") as type_otp, patch(
            "core.roxy_registration._click_continue"
        ), patch(
            "core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"
        ), patch("core.roxy_registration._fill_password_setup_page"):
            result = _run_roxy_password_setup(
                driver,
                "user@example.com",
                password="valid-password-123",
                previous_otp="111111",
        )

        self.assertEqual(result, "valid-password-123")
        resend.assert_not_called()
        self.assertNotIn("exclude_codes", wait_otp.call_args.kwargs)
        self.assertIn("after_ts", wait_otp.call_args.kwargs)
        self.assertIsNone(wait_otp.call_args.kwargs["otp_baseline"])
        type_otp.assert_called_once_with(driver, "222222")

    def test_password_setup_provider_timeout_uses_fresh_baseline_before_resend_and_second_wait(self):
        from core.roxy_registration import _run_roxy_password_setup
        from core.generic_api_mail_client import GenericApiMailError

        driver = object()
        events = []
        baselines = [object(), object()]

        def capture_baseline(email):
            baseline = baselines.pop(0)
            events.append(("baseline", email, baseline))
            return baseline

        def fetch_authorize_url(*_args):
            events.append(("authorize",))
            return "https://auth.openai.com/email-verification"

        def wait_for_password_otp(email, **kwargs):
            events.append(("wait", email, kwargs))
            if len([event for event in events if event[0] == "wait"]) == 1:
                raise GenericApiMailError("等待通用 API 验证码超时")
            return "222222"

        with patch("core.roxy_registration._fetch_password_setup_authorize_url", side_effect=fetch_authorize_url), patch(
            "core.roxy_registration._safe_get"
        ), patch(
            "core.roxy_registration._is_email_verification_page", return_value=True
        ), patch(
            "core.roxy_registration._click_resend_email_otp",
            side_effect=lambda *_args, **_kwargs: events.append(("resend",)),
        ), patch(
            "core.roxy_registration.resolve_email_source", return_value="generic_api"
        ), patch(
            "core.roxy_registration.capture_otp_baseline", side_effect=capture_baseline
        ), patch(
            "core.roxy_registration.time.time", side_effect=[100.0, 200.0]
        ), patch(
            "core.roxy_registration.wait_for_otp", side_effect=wait_for_password_otp
        ), patch("core.roxy_registration._type_otp") as type_otp, patch(
            "core.roxy_registration._click_continue"
        ), patch(
            "core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"
        ), patch("core.roxy_registration._fill_password_setup_page"):
            result = _run_roxy_password_setup(
                driver,
                "user@example.com",
                password="valid-password-123",
                previous_otp="111111",
            )

        self.assertEqual(result, "valid-password-123")
        self.assertEqual([event[0] for event in events], [
            "baseline", "authorize", "wait", "baseline", "resend", "wait",
        ])
        first_wait, second_wait = [event for event in events if event[0] == "wait"]
        self.assertEqual(first_wait[2], {"after_ts": 100.0, "otp_baseline": first_wait[2]["otp_baseline"]})
        self.assertEqual(second_wait[2], {"after_ts": 200.0, "otp_baseline": second_wait[2]["otp_baseline"]})
        self.assertIs(first_wait[2]["otp_baseline"], events[0][2])
        self.assertIs(second_wait[2]["otp_baseline"], events[3][2])
        self.assertNotIn("exclude_codes", first_wait[2])
        self.assertNotIn("exclude_codes", second_wait[2])
        type_otp.assert_called_once_with(driver, "222222")

    def test_add_password_request_uses_same_origin_authorize_flow(self):
        request = _build_password_setup_request(
            email="user+test@example.com",
            device_id="device-1",
            csrf_token="csrf-123",
            mode="post_login_add_password",
        )

        parsed = urlparse(request["url"])
        query = parse_qs(parsed.query)
        body = dict(parse_qsl(request["body"]))

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "chatgpt.com")
        self.assertEqual(parsed.path, "/api/auth/signin/openai")
        self.assertEqual(query["login_hint"], ["user+test@example.com"])
        self.assertEqual(query["post_login_add_password"], ["true"])
        self.assertEqual(query["ext-oai-did"], ["device-1"])
        self.assertEqual(body["csrfToken"], "csrf-123")
        self.assertEqual(body["callbackUrl"], "https://chatgpt.com/")
        self.assertEqual(body["json"], "true")

    def test_reset_password_request_uses_reset_mode_only(self):
        request = _build_password_setup_request(
            email="user@example.com",
            device_id="device-2",
            csrf_token="csrf-456",
            mode="post_login_password_reset",
        )
        query = parse_qs(urlparse(request["url"]).query)

        self.assertEqual(query["post_login_password_reset"], ["true"])
        self.assertNotIn("post_login_add_password", query)

    def test_password_setup_mode_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            _normalize_password_setup_mode("delete_account")

    def test_otp_supports_digit_aria_labels(self):
        class FakeElement:
            def __init__(self):
                self.sent = []

            def is_displayed(self):
                return True

            def is_enabled(self):
                return True

            def get_attribute(self, key):
                return {"aria-label": "Digit", "maxlength": "1"}.get(key, "")

            def send_keys(self, value):
                self.sent.append(value)

            def clear(self):
                self.sent.clear()

        class FakeDriver:
            def __init__(self):
                self.boxes = [FakeElement() for _ in range(6)]

            def find_elements(self, _by, selector):
                if selector == "input[aria-label*='digit' i]":
                    return self.boxes
                return []

        driver = FakeDriver()
        with patch("core.roxy_registration._browser_actions_enabled", return_value=False):
            _type_otp(driver, "123456")

        self.assertEqual([box.sent for box in driver.boxes], [["1"], ["2"], ["3"], ["4"], ["5"], ["6"]])

    def test_authorize_flow_has_browser_fetch_timeout_and_stage_result(self):
        class FakeDriver:
            current_url = "https://chatgpt.com/"

            def __init__(self):
                self.async_scripts = []

            def execute_script(self, _script):
                return "stable-device-id"

            def execute_async_script(self, script, *args):
                self.async_scripts.append(script)
                if len(self.async_scripts) == 1:
                    return {"ok": True, "status": 200, "data": {"csrfToken": "csrf-token"}}
                return {"ok": True, "status": 200, "data": {"url": "https://auth.openai.com/email-verification"}}

        driver = FakeDriver()
        result = _fetch_password_setup_authorize_url(driver, "user@example.com", "post_login_add_password")

        self.assertEqual(result, "https://auth.openai.com/email-verification")
        self.assertEqual(len(driver.async_scripts), 2)
        for script in driver.async_scripts:
            self.assertIn("AbortController", script)
            self.assertIn("setTimeout", script)
            self.assertIn("stage", script)


if __name__ == "__main__":
    unittest.main()
