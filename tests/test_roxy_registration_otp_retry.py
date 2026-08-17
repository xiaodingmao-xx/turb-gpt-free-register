# -*- coding: utf-8 -*-
from contextlib import ExitStack
from unittest.mock import patch

from core import roxy_registration as service
from core.roxybrowser_client import RoxyOpenResult


def test_registration_retry_excludes_code_rejected_by_verification_page():
    class FakeDriver:
        def set_page_load_timeout(self, _timeout):
            pass

        def set_script_timeout(self, _timeout):
            pass

        def quit(self):
            pass

    class FakeClient:
        def open_profile(self):
            return RoxyOpenResult("profile-otp-retry", {"code": 0})

        def cleanup_profile(self, _opened):
            pass

    with ExitStack() as stack:
        stack.enter_context(patch.object(service, "RoxyBrowserClient", return_value=FakeClient()))
        stack.enter_context(patch.object(service, "_build_driver", return_value=FakeDriver()))
        stack.enter_context(patch.object(service, "_center_browser_window"))
        stack.enter_context(patch.object(service, "_safe_get"))
        stack.enter_context(patch.object(service, "_submit_email_and_wait_next", return_value="otp"))
        stack.enter_context(patch.object(service, "_complete_profile_page", return_value=True))
        stack.enter_context(patch.object(
            service,
            "_fetch_chatgpt_session",
            return_value={"accessToken": "access-token", "user": {}, "account": {}, "expires": None},
        ))
        stack.enter_context(patch.object(service, "_type_otp"))
        stack.enter_context(patch.object(service, "_click_continue"))
        stack.enter_context(patch.object(
            service,
            "_wait_after_email_otp_submit",
            side_effect=["invalid", "accepted"],
        ))
        stack.enter_context(patch.object(service, "_click_resend_email_otp"))
        wait_otp = stack.enter_context(patch.object(service, "wait_for_otp", return_value="992669"))
        baseline = object()
        capture_baseline = stack.enter_context(
            patch.object(service, "capture_otp_baseline", return_value=baseline)
        )
        stack.enter_context(patch.object(service, "detect_selenium_exit_ip", return_value="203.0.113.9"))
        stack.enter_context(patch.object(service, "save_account_data", return_value=42))
        stack.enter_context(patch.object(service, "resolve_email_source", return_value="generic_api"))
        stack.enter_context(patch.object(service, "_check_manual_stop"))
        stack.enter_context(patch.object(service, "human_delay"))
        stack.enter_context(patch.object(service._cfg, "ROXY_PASSWORD_SETUP_ENABLED", False))
        stack.enter_context(patch.object(service._cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        stack.enter_context(patch.object(service._twofa_cfg, "ENABLE_2FA", False))
        stack.enter_context(patch("config.codex.ENABLE_CODEX_AUTO", False))
        result = service.run_roxy_registration(
            email="user@example.com",
            name="Test User",
            birthday="2000-01-01",
            otp_code=None,
        )

    assert result["success"] is True
    capture_baseline.assert_called_once_with("user@example.com")
    assert wait_otp.call_count == 2
    assert wait_otp.call_args.kwargs["exclude_codes"] == {"992669"}
    assert wait_otp.call_args.kwargs["otp_baseline"] is baseline
