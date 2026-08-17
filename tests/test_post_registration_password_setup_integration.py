# -*- coding: utf-8 -*-
"""Fake-only coverage for the registration-to-password-setup continuation."""
from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    OtpBaseline,
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
        from core import email_provider, roxy_registration
        from core import generic_api_mail_client

        otp = "".join(str(number) for number in range(1, 7))
        account = GenericApiEmailAccount("user@example.com", "https://mail.example/code")
        baseline = OtpBaseline(
            frozenset({otp}), frozenset({"registration-message"}), 1.0,
        )
        provider = _SameCodeNewMessageSession(otp)
        self.assertIs(roxy_registration.wait_for_otp, email_provider.wait_for_otp)
        with patch.object(email_provider, "resolve_email_source", return_value="generic_api"), patch(
            "core.generic_api_mail_client.get_account_context", return_value=account
        ), patch("core.generic_api_mail_client.requests.Session", return_value=provider), patch.object(
            generic_api_mail_client._email_cfg, "OTP_SETTLE_SECONDS", 0
        ), patch.object(
            generic_api_mail_client, "fetch_latest_otp", wraps=generic_api_mail_client.fetch_latest_otp
        ) as fetch_otp, patch.object(roxy_registration, "capture_otp_baseline", return_value=baseline), patch.object(
            roxy_registration, "_fetch_password_setup_authorize_url", return_value="https://auth.openai.com/email-verification"
        ), patch.object(roxy_registration, "_safe_get"), patch.object(
            roxy_registration, "_is_email_verification_page", return_value=True
        ), patch.object(
            roxy_registration, "_click_resend_email_otp"
        ) as resend, patch.object(roxy_registration, "_type_otp"), patch.object(
            roxy_registration, "_click_continue"
        ), patch.object(
            roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
        ), patch.object(roxy_registration, "_fill_password_setup_page"):
            saved_password = roxy_registration._run_roxy_password_setup(
                _FakeDriver(), account.email,
            )

        self.assertEqual(fetch_otp.call_count, 1)
        self.assertIn("after_ts", fetch_otp.call_args.kwargs)
        self.assertIs(fetch_otp.call_args.kwargs["otp_baseline"], baseline)
        self.assertTrue(saved_password)
        resend.assert_not_called()

    def test_timeout_handoffs_after_profile_cleanup_then_background_succeeds(self):
        """Inline timeout preserves registration success and hands it to one background queue."""
        from core import password_setup_task_service, registration_service, roxy_registration

        events = []
        registration_otp = "".join(str(number) for number in range(1, 7))
        background_password = "".join(chr(code) for code in (112, 97, 115, 115, 45, 111, 107))
        runner_results = []
        rows = [{
            "id": 47,
            "email": "user@example.com",
            "password_setup_status": "queued",
            "password_setup_attempt": 1,
            "password_setup_max_attempts": 3,
            "extra_json": json.dumps({"roxybrowser": {"profile_id": "background-profile"}}),
        }]

        class _InlineClient:
            def open_profile(self):
                return RoxyOpenResult("inline-profile", {"code": 0})

            def cleanup_profile(self, _opened):
                events.append("profile_cleanup")

        def run_inline_registration(**kwargs):
            events.append("runner_enter")
            with patch.object(roxy_registration, "RoxyBrowserClient", return_value=_InlineClient()), patch.object(
                roxy_registration, "_build_driver", return_value=_FakeDriver()
            ), patch.object(roxy_registration, "_center_browser_window"), patch.object(
                roxy_registration, "_safe_get"
            ), patch.object(
                roxy_registration, "_submit_email_and_wait_next", return_value="otp"
            ), patch.object(
                roxy_registration, "_wait_after_email_otp_submit", return_value="accepted"
            ), patch.object(roxy_registration, "_type_otp"), patch.object(
                roxy_registration, "_complete_profile_page", return_value=True
            ), patch.object(
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
                result = roxy_registration.run_roxy_registration(
                    email="user@example.com", name="Test User", birthday="2000-01-01", otp_code=registration_otp,
                )
            runner_results.append(result)
            return result

        def update_job(_job_id, **kwargs):
            if kwargs.get("status") == "success":
                events.append("registration_success")
            if kwargs.get("status") == "failed":
                events.append("registration_failed")

        def enqueue_background(**kwargs):
            events.append("handoff_enqueue")
            self.assertEqual(kwargs["account_id"], 47)
            self.assertTrue(password_setup_task_service._QUEUE_SLOTS.acquire(blocking=False))

            class _BackgroundClient:
                def cleanup_profile(self, _opened):
                    events.append("background_profile_cleanup")

            opened = RoxyOpenResult("background-profile", {"code": 0})
            with patch.object(password_setup_task_service.db, "_load_accounts", return_value=rows), patch.object(
                password_setup_task_service.db, "_save_accounts"
            ), patch.object(password_setup_task_service, "_append_password_setup_log"), patch(
                "core.roxybrowser_client.RoxyBrowserClient", return_value=_BackgroundClient()
            ), patch.object(
                password_setup_task_service, "_open_profile_with_recovery", return_value=opened
            ), patch.object(roxy_registration, "_build_driver", return_value=_FakeDriver()), patch.object(
                roxy_registration, "_run_roxy_password_setup", return_value=background_password
            ):
                result = password_setup_task_service._run_task_wrapper(
                    account_id=kwargs["account_id"],
                    email="user@example.com",
                    mode=kwargs["mode"],
                    password=kwargs["password"],
                )
            self.assertTrue(result["ok"])
            self.assertEqual(rows[0]["password_setup_status"], "success")
            self.assertTrue(json.loads(rows[0]["extra_json"]).get("registration_password"))
            self.assertNotIn(47, password_setup_task_service._ACTIVE)
            self.assertTrue(password_setup_task_service._QUEUE_SLOTS.acquire(blocking=False))
            password_setup_task_service._QUEUE_SLOTS.release()
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
            "main.run_registration", side_effect=run_inline_registration
        ), patch(
            "core.password_setup_task_service.enqueue_account_password_setup",
            side_effect=enqueue_background,
        ):
            registration_service._run_one_job(9, "ignored.log")

        self.assertEqual(len(runner_results), 1)
        self.assertTrue(runner_results[0]["success"])
        self.assertTrue(runner_results[0]["password_setup_handoff"])
        self.assertLess(events.index("runner_enter"), events.index("profile_cleanup"))
        self.assertLess(events.index("registration_success"), events.index("handoff_enqueue"))
        self.assertLess(events.index("profile_cleanup"), events.index("handoff_enqueue"))
        self.assertLess(events.index("handoff_enqueue"), events.index("background_success"))
        self.assertIn("registration_success", events)
        self.assertNotIn("registration_failed", events)
        self.assertEqual(rows[0]["password_setup_status"], "success")
        self.assertTrue(json.loads(rows[0]["extra_json"]).get("registration_password"))


if __name__ == "__main__":
    unittest.main()
