# -*- coding: utf-8 -*-
import unittest

from core.payment_method_detector import (
    detect_extract_payment_session,
    detect_oaics,
)


class PaymentMethodDetectorTests(unittest.TestCase):
    def test_detect_oaics_identifies_oaics_session(self):
        result = detect_oaics(
            {"checkout_session_id": "oaics_test123"},
            billing_country="JP",
        )
        self.assertTrue(result["detected"])
        self.assertTrue(result["is_oaics"])
        self.assertEqual(result["session_kind"], "oaics")

    def test_detect_oaics_identifies_stripe_cs_session_and_method(self):
        result = detect_oaics(
            {"id": "cs_test123"},
            {
                "payment_method_types": ["card", "paypal"],
                "currency": "jpy",
                "amount_due": 0,
            },
            billing_country="JP",
            expected_method="paypal",
        )
        self.assertEqual(result["session_kind"], "stripe_cs")
        self.assertEqual(result["method_status"], "available")
        self.assertEqual(result["offer_state"], "zero_due")

    def test_nested_extract_result_without_supported_session_is_not_detected(self):
        result = detect_extract_payment_session(
            {"result": {"id": "job-123", "payment_method": "pix"}}
        )
        self.assertFalse(result["detected"])
        self.assertEqual(result["session_kind"], "unknown")

    def test_nested_extract_result_detects_supported_session(self):
        result = detect_extract_payment_session(
            {
                "checkout": {"checkout_session_id": "oaics_nested123"},
                "stripe_init": {"payment_method_types": ["pix"]},
            },
            expected_method="pix",
        )
        self.assertTrue(result["detected"])
        self.assertEqual(result["session_kind"], "oaics")
        self.assertEqual(result["method_status"], "available")


if __name__ == "__main__":
    unittest.main()
