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
