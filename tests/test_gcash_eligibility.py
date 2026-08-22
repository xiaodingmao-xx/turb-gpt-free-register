# -*- coding: utf-8 -*-
import unittest


class GcashEligibilityTests(unittest.TestCase):
    def test_checkout_body_uses_configured_country_and_currency(self):
        from unittest.mock import patch

        from core import gcash_eligibility

        def configured(name, fallback):
            return {"GCASH_CHECK_COUNTRY": "VN", "GCASH_CHECK_CURRENCY": "VND"}.get(name, fallback)

        with patch.object(gcash_eligibility, "_setting", side_effect=configured):
            body = gcash_eligibility._checkout_body(0)

        self.assertEqual(body["billing_details"], {"country": "VN", "currency": "VND"})

    def test_gcash_available_when_checkout_and_stripe_init_list_gcash(self):
        from unittest.mock import patch

        from core import gcash_eligibility

        checkout = {
            "checkout_session_id": "cs_test_gcash",
            "one_click_trial_eligible": True,
            "checkout_session": {"subscription_data": {"trial_period_days": 0}},
        }
        with patch.object(gcash_eligibility, "_checkout_session", return_value=(checkout, "pk_test")), \
                patch.object(
                    gcash_eligibility,
                    "_stripe_init",
                    return_value={
                        "mode": "subscription",
                        "currency": "php",
                        "amount_due": 0,
                        "payment_method_types": ["card", "external_gcash"],
                    },
                ):
            result = gcash_eligibility.check_account_gcash("token", proxy="")

        self.assertEqual(result["decision"], "available")
        self.assertTrue(result["gcash_available"])
        self.assertEqual(result["payment_methods"], ["card", "gcash"])

    def test_network_failure_is_unknown_not_unavailable(self):
        from unittest.mock import patch

        from core import gcash_eligibility

        with patch.object(
            gcash_eligibility,
            "_checkout_session",
            side_effect=ConnectionError("network down"),
        ):
            result = gcash_eligibility.check_account_gcash(
                "token", proxy="", max_attempts=1
            )

        self.assertEqual(result["decision"], "unknown")
        self.assertIsNone(result["gcash_available"])
        self.assertFalse(result["conclusive"])

    def test_gcash_probe_never_logs_token_or_checkout_identifier(self):
        from unittest.mock import patch

        from core import gcash_eligibility

        messages = []
        with patch.object(
            gcash_eligibility,
            "_checkout_session",
            return_value=({"checkout_session_id": "cs_secret"}, "pk_secret"),
        ), patch.object(
            gcash_eligibility,
            "_stripe_init",
            return_value={"payment_method_types": ["gcash"]},
        ):
            result = gcash_eligibility.check_account_gcash(
                "eyJsecret-token", proxy="", progress_callback=messages.append
            )

        rendered = "\n".join(messages)
        self.assertNotIn("eyJsecret-token", rendered)
        self.assertNotIn("cs_secret", rendered)
        self.assertEqual(result["decision"], "available")


if __name__ == "__main__":
    unittest.main()
