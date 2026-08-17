# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from core import email_provider


class GPTMailProviderTests(unittest.TestCase):
    def test_capture_baseline_routes_only_generic_api(self):
        marker = object()
        with patch("core.email_provider.resolve_email_source", return_value="generic_api"), patch(
            "core.generic_api_mail_client.capture_otp_baseline", return_value=marker
        ) as capture:
            self.assertIs(email_provider.capture_otp_baseline("user@example.com"), marker)
        capture.assert_called_once_with("user@example.com")

    def test_capture_baseline_skips_non_generic_source(self):
        with patch("core.email_provider.resolve_email_source", return_value="outlook"):
            self.assertIsNone(email_provider.capture_otp_baseline("user@outlook.com"))

    @patch("core.generic_api_mail_client.fetch_latest_otp", return_value="107902")
    @patch("core.email_provider.resolve_email_source", return_value="generic_api")
    def test_wait_for_otp_passes_baseline_to_generic_api(self, _resolve, fetch_latest_otp):
        baseline = object()
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(
                email_provider.wait_for_otp(
                    "user@example.com",
                    after_ts=123.0,
                    otp_baseline=baseline,
                ),
                "107902",
            )
        fetch_latest_otp.assert_called_once_with(
            "user@example.com",
            after_ts=123.0,
            otp_baseline=baseline,
        )

    def test_parse_sources_keeps_gptmail_in_order(self):
        self.assertEqual(
            email_provider.parse_email_sources("outlook,gptmail,generic_api,mailnest,cloudmail"),
            ["outlook", "gptmail", "generic_api", "mailnest", "cloudmail"],
        )

    @patch("core.gptmail_client.pick_account")
    def test_acquire_email_uses_gptmail_client(self, pick_account):
        pick_account.return_value.email = "fresh@gptmail.test"

        with patch("core.email_provider.parse_email_sources", return_value=["gptmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@gptmail.test")

    @patch("core.generic_api_mail_client.pick_account")
    def test_acquire_email_can_use_explicit_source(self, pick_account):
        pick_account.return_value.email = "fresh@generic.test"

        with patch.object(email_provider, "parse_email_sources", return_value=["generic_api"]) as parse:
            self.assertEqual(
                email_provider.acquire_email("generic_api"),
                "fresh@generic.test",
            )
        parse.assert_called_once_with("generic_api")

    @patch("core.db.generic_api_email_pool_summary", return_value={"available": 0})
    def test_registration_email_pool_check_rejects_exhausted_generic_api_pool(self, _summary):
        result = email_provider.check_registration_email_pool("generic_api")

        self.assertFalse(result["ok"])
        self.assertEqual(result["available"], 0)
        self.assertEqual(result["sources"], ["generic_api"])

    @patch("core.gptmail_client.get_account_context", return_value=object())
    def test_resolve_email_source_recognizes_cached_gptmail_address(self, get_context):
        self.assertEqual(email_provider.resolve_email_source("fresh@gptmail.test"), "gptmail")
        get_context.assert_called_once_with("fresh@gptmail.test")

    @patch("core.gptmail_client.release_account")
    @patch("core.email_provider.resolve_email_source", return_value="gptmail")
    def test_release_email_clears_gptmail_context(self, resolve, release):
        self.assertEqual(email_provider.release_email("fresh@gptmail.test", status="failed"), "gptmail")
        release.assert_called_once_with("fresh@gptmail.test", status="failed", note=None)

    @patch("core.gptmail_client.fetch_latest_otp", return_value="654321")
    @patch("core.email_provider.resolve_email_source", return_value="gptmail")
    def test_wait_for_otp_uses_gptmail_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@gptmail.test", after_ts=123.0), "654321")
        fetch_latest_otp.assert_called_once_with("fresh@gptmail.test", after_ts=123.0)

    @patch("core.mailnest_client.pick_account")
    def test_acquire_email_uses_mailnest_client(self, pick_account):
        pick_account.return_value.email = "fresh@mailnest.test"

        with patch("core.email_provider.parse_email_sources", return_value=["mailnest"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@mailnest.test")

    @patch("core.mailnest_client.fetch_latest_otp", return_value="112233")
    @patch("core.email_provider.resolve_email_source", return_value="mailnest")
    def test_wait_for_otp_uses_mailnest_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@mailnest.test", after_ts=123.0), "112233")
        fetch_latest_otp.assert_called_once_with("fresh@mailnest.test", after_ts=123.0)

    @patch("core.cloudmail_client.pick_account")
    def test_acquire_email_uses_cloudmail_client(self, pick_account):
        pick_account.return_value.email = "fresh@cloudmail.test"

        with patch("core.email_provider.parse_email_sources", return_value=["cloudmail"]):
            self.assertEqual(email_provider.acquire_email(), "fresh@cloudmail.test")

    @patch("core.cloudmail_client.fetch_latest_otp", return_value="445566")
    @patch("core.email_provider.resolve_email_source", return_value="cloudmail")
    def test_wait_for_otp_uses_cloudmail_client(self, resolve, fetch_latest_otp):
        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            self.assertEqual(email_provider.wait_for_otp("fresh@cloudmail.test", after_ts=123.0), "445566")
        fetch_latest_otp.assert_called_once_with("fresh@cloudmail.test", after_ts=123.0)
