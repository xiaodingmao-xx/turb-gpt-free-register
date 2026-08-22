# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GcashCheckServiceTests(unittest.TestCase):
    def test_run_gcash_check_writes_detailed_phase_result_and_persist_logs(self):
        from core import gcash_check_service

        messages = []

        def append_log(email, line, **kwargs):
            messages.append(line)

        def fake_check(*args, **kwargs):
            kwargs["progress_callback"](
                "[GCash] phase=checkout_response http_status=200 payment_methods=['card','gcash']"
            )
            return {
                "ok": True,
                "conclusive": True,
                "decision": "available",
                "gcash_available": True,
                "trial_eligible": True,
                "actual_trial": False,
                "payment_methods": ["card", "gcash"],
                "payment_method_status": "available",
                "currency": "PHP",
                "amount_due": 0,
                "stripe_mode": "subscription",
                "http_status": 200,
                "attempt_count": 1,
                "max_attempts": 2,
                "retryable": False,
                "proxy_ip": "127.0.0.1",
                "error": None,
            }

        with patch.object(gcash_check_service.db, "mark_account_gcash_check_running", return_value=True), \
                patch.object(gcash_check_service.db, "update_account_gcash_check", return_value=True), \
                patch.object(gcash_check_service, "resolve_plan_check_route", return_value={
                    "proxy": "http://user:pass@127.0.0.1:7897",
                    "proxy_mode": "proxy",
                    "network_route": "proxy",
                    "proxy_used": "http://***:***@127.0.0.1:7897",
                }), \
                patch.object(gcash_check_service, "_wait_for_rate_slot"), \
                patch.object(gcash_check_service, "check_account_gcash", side_effect=fake_check), \
                patch.object(gcash_check_service, "_append_log", side_effect=append_log), \
                patch.object(gcash_check_service._QUEUE_SLOTS, "release"):
            result = gcash_check_service._run_gcash_check(
                account_id=146,
                email="user@example.com",
                access_token="secret-token",
                trigger="manual",
            )

        rendered = "\n".join(messages)
        self.assertTrue(result["ok"])
        self.assertIn("phase=worker_start", rendered)
        self.assertIn("phase=checkout_response", rendered)
        self.assertIn("phase=result", rendered)
        self.assertIn("phase=persist", rendered)
        self.assertIn("payment_methods", rendered)
        self.assertIn("proxy_ip=127.0.0.1", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("user:pass", rendered)

    def test_success_result_persists_only_derived_fields(self):
        from core import db

        rows = [{"id": 7, "email": "user@example.com", "access_token": "eyJsecret-token"}]
        result = {
            "ok": True,
            "conclusive": True,
            "decision": "available",
            "gcash_available": True,
            "trial_eligible": True,
            "actual_trial": False,
            "payment_methods": ["card", "gcash"],
            "payment_method_status": "available",
            "currency": "PHP",
            "amount_due": 0,
            "stripe_mode": "subscription",
            "http_status": 200,
            "network_route": "proxy",
            "proxy_used": "http://127.0.0.1:7897",
            "proxy_ip": "127.0.0.1",
            "error": None,
            "checked_at": "2026-08-22T12:00:00",
            "token": "eyJsecret-token",
            "checkout_session_id": "cs_secret",
        }
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            self.assertTrue(db.claim_account_gcash_check(acc_id=7, trigger="manual"))
            self.assertTrue(db.mark_account_gcash_check_running(7))
            self.assertTrue(db.update_account_gcash_check(acc_id=7, result=result))

        row = rows[0]
        self.assertEqual(row["gcash_check_status"], "success")
        self.assertTrue(row["gcash_available"])
        self.assertEqual(row["gcash_payment_methods"], ["card", "gcash"])
        persisted = row.get("gcash_check_result_json", "")
        self.assertNotIn("eyJsecret-token", persisted)
        self.assertNotIn("cs_secret", persisted)
        self.assertNotIn("checkout_session_id", persisted)

    def test_network_failure_keeps_gcash_unknown(self):
        from core import db

        rows = [{"id": 8, "email": "network@example.com", "gcash_available": True}]
        result = {
            "ok": False,
            "conclusive": False,
            "decision": "unknown",
            "gcash_available": None,
            "payment_methods": [],
            "payment_method_status": "unknown",
            "error": "ConnectionError: network down",
            "checked_at": "2026-08-22T12:00:00",
        }
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            self.assertTrue(db.update_account_gcash_check(acc_id=8, result=result))

        self.assertTrue(rows[0]["gcash_available"])
        self.assertEqual(rows[0]["gcash_check_decision"], "unknown")
        self.assertIsNone(rows[0]["gcash_check_ok"])

    def test_duplicate_submission_is_rejected(self):
        from core import gcash_check_service

        with patch.object(gcash_check_service._QUEUE_SLOTS, "acquire", return_value=True), \
                patch.object(gcash_check_service._QUEUE_SLOTS, "release") as release, \
                patch.object(gcash_check_service.db, "claim_account_gcash_check", return_value=False):
            result = gcash_check_service.enqueue_account_gcash_check(
                9, "busy@example.com", "token", trigger="manual"
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["busy"])
        release.assert_called_once()

    def test_queue_snapshot_exposes_running_account_and_queued_count(self):
        from core import gcash_check_service

        with patch.object(
            gcash_check_service.db,
            "list_account_gcash_check_statuses",
            return_value={
                "items": [
                    {"id": 1, "email": "running@example.com", "gcash_check_status": "running"},
                    {"id": 2, "email": "queued@example.com", "gcash_check_status": "queued"},
                    {"id": 3, "email": "done@example.com", "gcash_check_status": "success"},
                ]
            },
        ):
            snapshot = gcash_check_service.queue_status()

        self.assertEqual(snapshot["running_count"], 1)
        self.assertEqual(snapshot["queued_count"], 1)
        self.assertEqual(snapshot["running"][0]["email"], "running@example.com")
        self.assertEqual(snapshot["queued"][0]["email"], "queued@example.com")

    def test_gcash_log_redacts_sensitive_values(self):
        from core import gcash_check_service

        rendered = gcash_check_service.safe_gcash_log_text(
            "token=eyJsecret-token proxy=http://u:p@127.0.0.1:7897 "
            "checkout_session_id=cs_secret publishable_key=pk_live_secret"
        )
        self.assertNotIn("eyJsecret-token", rendered)
        self.assertNotIn("u:p@", rendered)
        self.assertNotIn("cs_secret", rendered)
        self.assertNotIn("pk_live_secret", rendered)

    def test_log_path_is_email_safe(self):
        from core import gcash_check_service

        with tempfile.TemporaryDirectory() as td:
            with patch.object(gcash_check_service, "_LOG_DIR", Path(td)):
                gcash_check_service._append_log("person/a@example.com", "[GCash] test", clear=True)
                path = gcash_check_service.log_path("person/a@example.com")
                self.assertEqual(path.name, "gcash-check-person_a@example.com.log")
                self.assertIn("[GCash] test", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
