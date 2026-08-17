# -*- coding: utf-8 -*-
"""Fake-only coverage for the registration-to-password-setup continuation."""
from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    OtpBaseline,
    fetch_latest_otp,
)
from core.roxybrowser_client import RoxyOpenResult


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self.text = json.dumps(payload)


class _SameCodeNewMessageSession:
    def __init__(self, otp):
        self.otp = otp

    def get(self, _url, **_kwargs):
        return _FakeResponse({
            "code": self.otp,
            "message_id": "password-message",
        })


class _FakeDriver:
    current_url = "https://auth.openai.com/email-verification"

    def set_page_load_timeout(self, _timeout):
        pass

    def set_script_timeout(self, _timeout):
        pass

    def quit(self):
        pass


class PostRegistrationPasswordSetupIntegrationTests(unittest.TestCase):
    def test_late_same_code_message_completes_inline_password_setup(self):
        """A new mail identity may reuse a prior code without an immediate resend."""
        from core import roxy_registration

        otp = "".join(str(number) for number in range(1, 7))
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        baseline = OtpBaseline(
            frozenset({otp}), frozenset({"registration-message"}), 1.0,
        )
        provider = _SameCodeNewMessageSession(otp)
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), patch(
            "core.generic_api_mail_client.requests.Session", return_value=provider
        ), patch("core.generic_api_mail_client.time.sleep"):
            password_setup_otp = fetch_latest_otp(
                account.email,
                after_ts=100.0,
                max_wait=1,
                poll_interval=1,
                settle_seconds=0,
                otp_baseline=baseline,
            )

        resend = None
        with patch.object(roxy_registration, "capture_otp_baseline", return_value=baseline), patch.object(
            roxy_registration, "_fetch_password_setup_authorize_url", return_value="https://auth.openai.com/email-verification"
        ), patch.object(roxy_registration, "_safe_get"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ), patch.object(roxy_registration, "wait_for_otp", return_value=password_setup_otp), patch.object(
            roxy_registration, "_click_resend_email_otp"
        ) as resend, patch.object(roxy_registration, "_type_otp"), patch.object(
            roxy_registration, "_click_continue"
        ), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_fill_password_setup_page"):
            saved_password = roxy_registration._run_roxy_password_setup(
                _FakeDriver(), account.email,
            )

        self.assertEqual(password_setup_otp, otp)
        self.assertTrue(saved_password)
        resend.assert_not_called()

    def test_timeout_handoffs_after_profile_cleanup_then_background_succeeds(self):
        """Inline timeout preserves registration success and hands it to one background queue."""
        from core import registration_service, roxy_registration

        events = []
        account_state = {"password_setup_status": "failed"}
        registration_otp = "".join(str(number) for number in range(1, 7))

        class _InlineClient:
            def open_profile(self):
                return RoxyOpenResult("inline-profile", {"code": 0})

            def cleanup_profile(self, _opened):
                events.append("profile_cleanup")

        with patch.object(roxy_registration, "RoxyBrowserClient", return_value=_InlineClient()), patch.object(
            roxy_registration, "_build_driver", return_value=_FakeDriver()
        ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
            roxy_registration, "_safe_get"
        ), patch.object(
            roxy_registration, "_submit_email_and_wait_next", return_value="otp"
        ), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_type_otp"
        ), patch.object(roxy_registration, "_complete_profile_page", return_value=True), patch.object(
            roxy_registration, "_fetch_chatgpt_session",
            return_value={"accessToken": "token", "user": {}, "account": {}, "expires": None},
        ), patch.object(
            roxy_registration, "_run_password_setup_with_gate", side_effect=TimeoutError("otp wait elapsed")
        ), patch.object(roxy_registration, "detect_selenium_exit_ip", return_value="203.0.113.9"), patch.object(
            roxy_registration, "save_account_data", return_value=47
        ), patch.object(roxy_registration, "resolve_email_source", return_value="generic_api"), patch.object(
            roxy_registration, "_check_manual_stop"
        ), patch.object(roxy_registration, "human_delay"), patch.object(
            roxy_registration._cfg, "ROXY_PASSWORD_SETUP_ENABLED", True
        ), patch.object(roxy_registration._cfg, "ROXY_KEEP_BROWSER_OPEN", False), patch.object(
            roxy_registration._twofa_cfg, "ENABLE_2FA", False
        ), patch("config.codex.ENABLE_CODEX_AUTO", False):
            inline_result = roxy_registration.run_roxy_registration(
                email="user@example.com", name="Test User", birthday="2000-01-01", otp_code=registration_otp,
            )

        def update_job(_job_id, **kwargs):
            if kwargs.get("status") == "success":
                events.append("registration_success")
            if kwargs.get("status") == "failed":
                events.append("registration_failed")

        def enqueue_background(**kwargs):
            events.append("handoff_enqueue")
            self.assertEqual(kwargs["account_id"], 47)
            account_state["password_setup_status"] = "success"
            events.append("background_success")
            return {"accepted": True}

        with patch.object(registration_service, "_activate_job"), patch.object(
            registration_service, "_deactivate_job"
        ), patch.object(registration_service, "_JobLogContext", return_value=nullcontext()), patch.object(
            registration_service, "_prepare_registration_args", return_value=("user@example.com", "Test User", "2000-01-01")
        ), patch.object(registration_service, "is_stop_requested", return_value=False), patch.object(
            registration_service, "check_stop_requested"
        ), patch.object(registration_service, "record_registration_success"), patch.object(
            registration_service.db, "get_job", return_value={"id": 9, "status": "pending"}
        ), patch.object(registration_service.db, "update_job", side_effect=update_job), patch(
            "main.run_registration", return_value=inline_result
        ), patch(
            "core.password_setup_task_service.enqueue_account_password_setup",
            side_effect=enqueue_background,
        ):
            registration_service._run_one_job(9, "ignored.log")

        self.assertTrue(inline_result["success"])
        self.assertTrue(inline_result["password_setup_handoff"])
        self.assertLess(events.index("profile_cleanup"), events.index("handoff_enqueue"))
        self.assertIn("registration_success", events)
        self.assertNotIn("registration_failed", events)
        self.assertEqual(account_state["password_setup_status"], "success")


if __name__ == "__main__":
    unittest.main()
