# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import roxy_twofa


class _Driver:
    current_url = "https://chatgpt.com/"


class RoxyTwoFATests(unittest.TestCase):
    def test_redaction_removes_otp_token_and_secret(self):
        text = roxy_twofa.redact_twofa_error(
            'otp=123456 authorization=Bearer abc.def secret="JBSWY3DPEHPK3PXP"'
        )
        self.assertNotIn("123456", text)
        self.assertNotIn("abc.def", text)
        self.assertNotIn("JBSWY3DPEHPK3PXP", text)

    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "token"})
    @patch("core.roxy_twofa._complete_reauth_otp")
    @patch("core.roxy_twofa._fetch_reauth_authorize_url", return_value="https://auth.openai.com/authorize")
    @patch("core.roxy_twofa._mfa_request")
    def test_setup_enrolls_and_activates_before_returning_secret(
        self, request, _authorize, _otp, _session
    ):
        request.side_effect = [
            {"ok": True, "status": 200, "data": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"}},
            {"ok": True, "status": 200, "data": {"success": True}},
        ]
        result = roxy_twofa.setup_existing_account_2fa(_Driver(), "user@example.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(request.call_count, 2)

    @patch("core.roxy_twofa.time.sleep")
    @patch("core.roxy_registration._fetch_chatgpt_session", return_value={"accessToken": "token"})
    @patch("core.roxy_twofa._complete_reauth_otp")
    @patch("core.roxy_twofa._fetch_reauth_authorize_url", return_value="https://auth.openai.com/authorize")
    @patch("core.roxy_twofa._mfa_request")
    def test_uncertain_activation_does_not_enroll_again(
        self, request, _authorize, _otp, _session, _sleep
    ):
        request.side_effect = [
            {"ok": True, "status": 200, "data": {"secret": "JBSWY3DPEHPK3PXP", "session_id": "sid"}},
            {"ok": False, "status": 503, "data": {}},
        ]
        with self.assertRaises(roxy_twofa.TwoFAEnrollmentUncertain):
            roxy_twofa.setup_existing_account_2fa(_Driver(), "user@example.com")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
