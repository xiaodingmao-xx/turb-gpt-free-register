# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlparse
from unittest.mock import Mock, call, patch

import pyotp

from config import codex as codex_config
from core import roxy_registration as roxy_registration_module
from core.roxy_registration import (
    _build_twofa_setup_request,
    _fetch_twofa_authorize_url,
    _run_roxy_twofa_setup,
    run_roxy_registration,
)


class RoxyTwoFaTests(unittest.TestCase):
    def test_reauth_request_targets_authenticator_totp_callback(self):
        request_data = _build_twofa_setup_request(
            "user+test@example.com",
            "device-1",
            "csrf-123",
        )

        parsed = urlparse(request_data["url"])
        query = parse_qs(parsed.query)
        body = dict(parse_qsl(request_data["body"]))

        self.assertEqual(parsed.netloc, "chatgpt.com")
        self.assertEqual(parsed.path, "/api/auth/signin/openai")
        self.assertEqual(query["login_hint"], ["user+test@example.com"])
        self.assertEqual(query["reauth"], ["password"])
        self.assertEqual(query["max_age"], ["0"])
        self.assertEqual(query["ext-oai-did"], ["device-1"])
        self.assertEqual(body["csrfToken"], "csrf-123")
        self.assertEqual(body["callbackUrl"], "https://chatgpt.com/?action=enable&factor=totp")
        self.assertEqual(body["json"], "true")

    def test_authorize_url_is_created_inside_current_roxy_session(self):
        class FakeDriver:
            current_url = "https://chatgpt.com/"

            def __init__(self):
                self.scripts = []

            def execute_async_script(self, script, *args):
                self.scripts.append((script, args))
                if len(self.scripts) == 1:
                    return {"ok": True, "status": 200, "data": {"csrfToken": "csrf-token"}}
                return {
                    "ok": True,
                    "status": 200,
                    "data": {"url": "https://auth.openai.com/email-verification"},
                }

        driver = FakeDriver()
        authorize_url, device_id = _fetch_twofa_authorize_url(
            driver,
            "user@example.com",
            device_id="stable-device-id",
        )

        self.assertEqual(authorize_url, "https://auth.openai.com/email-verification")
        self.assertEqual(device_id, "stable-device-id")
        self.assertEqual(len(driver.scripts), 2)
        for script, _args in driver.scripts:
            self.assertIn("AbortController", script)
            self.assertIn("setTimeout", script)

    def test_full_setup_enrolls_and_activates_totp(self):
        driver = object()
        session_info = {"accessToken": "fresh-token", "user": {"email": "user@example.com"}}
        secret = pyotp.random_base32()

        with patch(
            "core.roxy_registration._fetch_twofa_authorize_url",
            return_value=("https://auth.openai.com/email-verification", "device-1"),
        ), patch("core.roxy_registration._safe_get"), patch(
            "core.roxy_registration._is_email_verification_page", return_value=True
        ), patch("core.roxy_registration.wait_for_otp", return_value="123456"), patch(
            "core.roxy_registration._clear_otp_inputs"
        ), patch("core.roxy_registration._type_otp") as type_otp, patch(
            "core.roxy_registration._click_continue"
        ), patch(
            "core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"
        ), patch(
            "core.roxy_registration._fetch_chatgpt_session", return_value=session_info
        ), patch(
            "core.roxy_registration._post_chatgpt_twofa_api",
            side_effect=[
                {"secret": secret, "session_id": "enrollment-1"},
                {"success": True},
            ],
        ) as post_api, patch("core.roxy_registration._check_manual_stop"):
            actual_secret, actual_session = _run_roxy_twofa_setup(driver, "user@example.com")

        self.assertEqual(actual_secret, secret)
        self.assertEqual(actual_session, session_info)
        type_otp.assert_called_once_with(driver, "123456")
        self.assertEqual(post_api.call_count, 2)
        self.assertEqual(
            post_api.call_args_list[0],
            call(
                driver,
                path="/backend-api/accounts/mfa/enroll",
                access_token="fresh-token",
                device_id="device-1",
                payload={"factor_type": "totp"},
                stage="enroll",
            ),
        )
        activation_payload = post_api.call_args_list[1].kwargs["payload"]
        self.assertEqual(activation_payload["factor_type"], "totp")
        self.assertEqual(activation_payload["session_id"], "enrollment-1")
        self.assertRegex(activation_payload["code"], r"^\d{6}$")

    def test_roxy_registration_requires_twofa_and_persists_secret(self):
        class FakeDriver:
            current_url = "https://chatgpt.com/"

            def set_page_load_timeout(self, _timeout):
                return None

            def set_script_timeout(self, _timeout):
                return None

            def quit(self):
                return None

        opened = SimpleNamespace(profile_id="profile-1", raw={"data": {}}, created_by_run=True)
        fake_client = Mock()
        fake_client.open_profile.return_value = opened
        initial_session = {
            "accessToken": "initial-token",
            "user": {"email": "user@example.com", "mfa": False},
            "account": {"planType": "free"},
            "expires": "2099-01-01T00:00:00Z",
        }
        refreshed_session = {
            **initial_session,
            "accessToken": "fresh-token-after-reauth",
            "user": {"email": "user@example.com", "mfa": True},
        }

        with ExitStack() as stack:
            stack.enter_context(patch("core.roxy_registration.RoxyBrowserClient", return_value=fake_client))
            stack.enter_context(patch("core.roxy_registration._build_driver", return_value=FakeDriver()))
            stack.enter_context(patch("core.roxy_registration._center_browser_window"))
            stack.enter_context(patch("core.roxy_registration._safe_get"))
            stack.enter_context(patch("core.roxy_registration.human_delay"))
            stack.enter_context(patch("core.roxy_registration._page_warmup"))
            stack.enter_context(patch("core.roxy_registration._maybe_accept"))
            stack.enter_context(patch("core.roxy_registration._submit_email_and_wait_next", return_value="otp"))
            stack.enter_context(patch("core.roxy_registration.wait_for_otp", return_value="123456"))
            stack.enter_context(patch("core.roxy_registration._clear_otp_inputs"))
            stack.enter_context(patch("core.roxy_registration._type_otp"))
            stack.enter_context(patch("core.roxy_registration._click_continue"))
            stack.enter_context(patch("core.roxy_registration._wait_after_email_otp_submit", return_value="accepted"))
            stack.enter_context(patch("core.roxy_registration._complete_profile_page", return_value=True))
            stack.enter_context(patch("core.roxy_registration._fetch_chatgpt_session", return_value=initial_session))
            setup_twofa = stack.enter_context(patch(
                "core.roxy_registration._run_roxy_twofa_setup",
                return_value=("TOTP-SECRET", refreshed_session),
            ))
            save_account = stack.enter_context(patch("core.roxy_registration.save_account_data", return_value=42))
            stack.enter_context(patch("core.roxy_registration.resolve_email_source", return_value="outlook"))
            stack.enter_context(patch("core.roxy_registration._check_manual_stop"))
            stack.enter_context(patch.object(roxy_registration_module._twofa_cfg, "ENABLE_2FA", True))
            stack.enter_context(patch.object(roxy_registration_module._cfg, "ROXY_PASSWORD_SETUP_ENABLED", False))
            stack.enter_context(patch.object(roxy_registration_module._cfg, "ROXY_KEEP_BROWSER_OPEN", False))
            stack.enter_context(patch.object(codex_config, "ENABLE_CODEX_AUTO", False))
            result = run_roxy_registration(
                email="user@example.com",
                name="Test User",
                birthday="1995-01-02",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], 42)
        self.assertEqual(result["access_token"], "fresh-token-after-reauth")
        self.assertEqual(result["totp_secret"], "TOTP-SECRET")
        setup_twofa.assert_called_once()
        self.assertEqual(setup_twofa.call_args.args[1], "user@example.com")
        self.assertEqual(save_account.call_args.kwargs["access_token"], "fresh-token-after-reauth")
        self.assertEqual(save_account.call_args.kwargs["totp_secret"], "TOTP-SECRET")
        self.assertTrue(save_account.call_args.kwargs["extra"]["user"]["mfa"])
        fake_client.cleanup_profile.assert_called_once_with(opened)


if __name__ == "__main__":
    unittest.main()
