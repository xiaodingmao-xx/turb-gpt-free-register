# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.roxy_codex_oauth import (
    _is_totp_challenge_state,
    _submit_totp_challenge_if_present,
    run_roxy_codex_oauth,
)


def _challenge_state(*, url: str = "https://auth.openai.com/mfa/totp") -> dict:
    return {
        "url": url,
        "title": "Verify your identity",
        "bodyText": "Enter the code from your authenticator app",
        "inputs": [{
            "type": "text",
            "name": "totp",
            "id": "totp-code",
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "ariaLabel": "Authenticator code",
            "placeholder": "Code",
            "maxLength": 6,
            "visible": True,
            "disabled": False,
        }],
        "errors": [],
    }


class RoxyTwoFaLoginTests(unittest.TestCase):
    def test_classifier_detects_authenticator_challenge(self):
        self.assertTrue(_is_totp_challenge_state(_challenge_state()))

    def test_classifier_rejects_email_and_phone_otp_pages(self):
        for url in (
            "https://auth.openai.com/email-verification",
            "https://auth.openai.com/phone-verification",
            "https://auth.openai.com/add-phone",
        ):
            with self.subTest(url=url):
                self.assertFalse(_is_totp_challenge_state(_challenge_state(url=url)))

    def test_classifier_rejects_generic_mfa_sms_and_email_challenges(self):
        for body in (
            "Enter the verification code we sent to your phone by text message",
            "Enter the code we sent to your email",
        ):
            state = _challenge_state(url="https://auth.openai.com/mfa/challenge")
            state["bodyText"] = body
            state["inputs"][0].update({"name": "code", "id": "code", "ariaLabel": "Code"})
            with self.subTest(body=body):
                self.assertFalse(_is_totp_challenge_state(state))

    def test_authenticator_code_is_generated_and_submitted(self):
        driver = object()
        next_state = {
            "url": "https://auth.openai.com/oauth/authorize",
            "title": "Authorize",
            "bodyText": "Continue to Codex",
            "inputs": [],
            "errors": [],
        }
        with patch(
            "core.roxy_codex_oauth._totp_challenge_state",
            side_effect=[_challenge_state(), next_state],
        ), patch("core.roxy_codex_oauth._clear_otp_inputs") as clear_inputs, patch(
            "core.roxy_codex_oauth._type_otp"
        ) as type_otp, patch(
            "core.roxy_codex_oauth._click_if_present", return_value=True
        ) as click_submit, patch("core.roxy_codex_oauth.human_delay"):
            handled = _submit_totp_challenge_if_present(
                driver,
                "user@example.com",
                totp_secret="JBSWY3DPEHPK3PXP",
                detect_timeout=1,
            )

        self.assertTrue(handled)
        clear_inputs.assert_called_once_with(driver)
        submitted_code = type_otp.call_args.args[1]
        self.assertRegex(submitted_code, r"^\d{6}$")
        click_submit.assert_called_once()

    def test_missing_saved_secret_fails_with_actionable_error(self):
        with patch(
            "core.roxy_codex_oauth._totp_challenge_state", return_value=_challenge_state()
        ), patch("core.roxy_codex_oauth.db.get_account_by_email", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "未保存 totp_secret"):
                _submit_totp_challenge_if_present(
                    object(),
                    "user@example.com",
                    detect_timeout=1,
                )

    def test_no_challenge_returns_without_reading_secret(self):
        state = {
            "url": "https://auth.openai.com/oauth/authorize",
            "title": "Authorize",
            "bodyText": "Continue",
            "inputs": [],
            "errors": [],
        }
        with patch(
            "core.roxy_codex_oauth._totp_challenge_state", return_value=state
        ), patch("core.roxy_codex_oauth.db.get_account_by_email") as get_account:
            handled = _submit_totp_challenge_if_present(
                object(),
                "user@example.com",
                detect_timeout=1,
            )

        self.assertFalse(handled)
        get_account.assert_not_called()

    def test_codex_wrapper_forwards_explicit_secret(self):
        with patch(
            "core.roxy_codex_oauth._run_roxy_codex_oauth_once",
            return_value={"ok": True, "status": "success"},
        ) as run_once:
            result = run_roxy_codex_oauth(
                "user@example.com",
                force=True,
                totp_secret="JBSWY3DPEHPK3PXP",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(run_once.call_args.kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    unittest.main()
