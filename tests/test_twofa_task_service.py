# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core import twofa_task_service as service


class TwoFATaskServiceTests(unittest.TestCase):
    @patch.object(service, "_append_log")
    @patch.object(service._EXECUTOR, "submit", return_value=MagicMock())
    @patch.object(service.db, "claim_account_twofa_setup", return_value=True)
    @patch.object(service.db, "get_account", return_value={"id": 7, "email": "user@example.com"})
    def test_enqueue_accepts_existing_account(self, _get, claim, submit, _log):
        with patch.object(service._QUEUE_SLOTS, "acquire", return_value=True):
            result = service.enqueue_account_twofa(account_id=7)
        self.assertTrue(result["accepted"])
        claim.assert_called_once()
        submit.assert_called_once()

    @patch.object(service.db, "get_account", return_value={
        "id": 7, "email": "user@example.com", "totp_secret": "SECRET",
    })
    def test_enqueue_skips_account_with_secret(self, _get):
        result = service.enqueue_account_twofa(account_id=7)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["accepted"])

    def test_enrollment_uncertain_is_not_retryable(self):
        from core.roxy_twofa import TwoFAEnrollmentUncertain
        self.assertFalse(service._retryable(TwoFAEnrollmentUncertain("unknown")))

    @patch.object(service, "setup_existing_account_2fa")
    @patch.object(service, "open_account_profile_with_recovery", return_value=SimpleNamespace(
        profile_id="p-1", created_by_run=False,
    ))
    @patch("core.roxy_registration.login_existing_account_with_otp", return_value={
        "user": {"email": "other@example.com", "mfa": False},
    })
    @patch("core.roxy_registration._build_driver", return_value=MagicMock())
    @patch("core.roxybrowser_client.RoxyBrowserClient")
    @patch.object(service.db, "update_account_twofa_setup_phase", return_value=True)
    @patch.object(service.db, "update_account_twofa_setup", return_value=True)
    @patch.object(service.db, "get_account", return_value={
        "id": 7, "email": "user@example.com", "twofa_setup_attempt": 1,
    })
    @patch.object(service.db, "mark_account_twofa_setup_running", return_value=True)
    @patch.object(service, "_append_log")
    def test_task_rejects_profile_logged_into_different_account(
        self, _log, _running, _get, update, _phase, _client, _driver,
        _login, _open, setup,
    ):
        result = service._run_twofa_task(account_id=7, email="user@example.com")
        self.assertFalse(result["ok"])
        self.assertIn("不一致", result["error"])
        setup.assert_not_called()
        update.assert_called()


if __name__ == "__main__":
    unittest.main()
