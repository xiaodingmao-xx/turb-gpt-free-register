# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db
from core.chatgpt_plan import _detect_plan_exit_geo, parse_accounts_check


class ChatgptPlanTests(unittest.TestCase):
    def test_parse_accounts_check_extracts_explicit_eligibility_region(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acct-1", "plan_type": "free"},
                    "eligible_promo_campaigns": {"plus": {"id": "plus-1"}},
                    "country": "JP",
                    "region": "Kanto",
                }
            }
        }
        result = parse_accounts_check(payload)
        self.assertEqual(result["plan_eligibility_country"], "JP")
        self.assertEqual(result["plan_eligibility_region"], "Kanto")
        self.assertEqual(result["plan_eligibility_region_source"], "accounts_check")

    def test_parse_accounts_check_does_not_infer_missing_eligibility_region(self):
        payload = {"accounts": {"default": {"account": {"plan_type": "free"}}}}
        result = parse_accounts_check(payload)
        self.assertIsNone(result.get("plan_eligibility_country"))
        self.assertIsNone(result.get("plan_eligibility_region"))

    def test_exit_geo_normalizes_country_and_location(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "ip": "203.0.113.10",
                    "country": "jp",
                    "region": "Kanto",
                    "city": "Tokyo",
                    "timezone": "Asia/Tokyo",
                }

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        result = _detect_plan_exit_geo(Session())
        self.assertEqual(result["plan_exit_ip"], "203.0.113.10")
        self.assertEqual(result["plan_exit_country"], "JP")
        self.assertEqual(result["plan_exit_region"], "Kanto")
        self.assertEqual(result["plan_exit_city"], "Tokyo")
        self.assertEqual(result["plan_exit_timezone"], "Asia/Tokyo")
        self.assertEqual(result["plan_exit_geo_source"], "ipinfo.io")

    def test_update_account_plan_check_saves_region_and_exit_fields(self):
        rows = [{"id": 7, "email": "user@example.com"}]
        result = {
            "ok": True,
            "checked_at": "2026-08-13T12:00:00",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
            "plan_eligibility_country": "JP",
            "plan_eligibility_region": "Kanto",
            "plan_eligibility_region_source": "accounts_check",
            "plan_exit_ip": "203.0.113.10",
            "plan_exit_country": "JP",
            "plan_exit_region": "Kanto",
            "plan_exit_city": "Tokyo",
            "plan_exit_timezone": "Asia/Tokyo",
            "plan_exit_geo_source": "ipinfo.io",
        }
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_plan_check(7, result=result))
        self.assertEqual(rows[0]["plan_eligibility_country"], "JP")
        self.assertEqual(rows[0]["plan_exit_city"], "Tokyo")

    def test_plan_region_and_exit_fields_are_part_of_result_contract(self):
        payload = {
            "accounts": {
                "default": {
                    "account": {"account_id": "acct-1", "plan_type": "free"},
                    "residencyRegion": "JP",
                }
            }
        }
        result = parse_accounts_check(payload)
        self.assertEqual(result["plan_eligibility_region"], "JP")


if __name__ == "__main__":
    unittest.main()
