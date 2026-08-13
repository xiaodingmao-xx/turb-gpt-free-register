# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db


class ExtractLinkPaymentDetectionTests(unittest.TestCase):
    def test_update_account_extract_saves_payment_session_fields(self):
        rows = [{"id": 7, "email": "user@example.com"}]
        result = {
            "ok": True,
            "status": "success",
            "result": {"long_url": "https://example.test/pay"},
            "payment_detection": {
                "detected": True,
                "checkout_session_id": "oaics_test123",
                "session_kind": "oaics",
                "is_oaics": True,
                "method_status": "available",
                "method_available": True,
                "payment_method_types": ["paypal"],
                "currency": "JPY",
                "amount_minor": 0,
                "offer_state": "zero_due",
            },
        }

        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_extract(7, result))

        self.assertEqual(rows[0]["extract_link_payment_session_kind"], "oaics")
        self.assertTrue(rows[0]["extract_link_payment_is_oaics"])
        self.assertTrue(rows[0]["extract_link_payment_detected"])
        self.assertEqual(rows[0]["extract_link_payment_methods"], "[\"paypal\"]")


if __name__ == "__main__":
    unittest.main()
