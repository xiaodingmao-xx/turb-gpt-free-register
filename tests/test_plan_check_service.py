# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import plan_check_service


class PlanCheckServiceTests(unittest.TestCase):
    def test_run_plan_check_writes_phase_result_and_persist_logs(self):
        messages = []

        def append_log(email, line, **kwargs):
            messages.append(line)

        def fake_check(*args, **kwargs):
            kwargs["progress_callback"](
                "[Plan] phase=response http_status=200 response_summary=accounts=1"
            )
            return {
                "ok": True,
                "http_status": 200,
                "current_plan_type": "free",
                "plus_trial_eligible": False,
                "attempt_count": 1,
                "max_attempts": 2,
                "retryable": False,
                "error": None,
                "proxy_ip": "127.0.0.1",
                "plan_exit_ip": "203.0.113.10",
                "plan_exit_country": "JP",
            }

        with patch.object(plan_check_service.db, "mark_account_plan_check_running", return_value=True), \
                patch.object(plan_check_service.db, "update_account_plan_check", return_value=True), \
                patch.object(plan_check_service, "_wait_for_rate_slot"), \
                patch.object(plan_check_service, "_registration_recheck_delay", return_value=0), \
                patch.object(plan_check_service, "check_account_plan", side_effect=fake_check), \
                patch.object(plan_check_service, "_append_log", side_effect=append_log), \
                patch.object(plan_check_service._QUEUE_SLOTS, "release"):
            result = plan_check_service._run_plan_check(
                account_id=146,
                email="user@example.com",
                access_token="secret-token",
                trigger="manual_bulk",
                proxy="http://user:pass@127.0.0.1:7897",
                timezone_offset_min="-480",
            )

        rendered = "\n".join(messages)
        self.assertTrue(result["ok"])
        self.assertIn("phase=worker_start", rendered)
        self.assertIn("phase=response", rendered)
        self.assertIn("phase=result", rendered)
        self.assertIn("phase=persist", rendered)
        self.assertIn("plan=free", rendered)
        self.assertIn("plus_trial_eligible=false", rendered)
        self.assertIn("proxy_ip=127.0.0.1", rendered)
        self.assertIn("exit_ip=203.0.113.10", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("user:pass", rendered)

    def test_plan_log_path_and_append_log_keep_email_safe(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(plan_check_service, "_LOG_DIR", Path(td)):
                plan_check_service._append_log("person/a@example.com", "[Plan] test", clear=True)
                path = plan_check_service.log_path("person/a@example.com")
                self.assertEqual(path.name, "plan-check-person_a@example.com.log")
                self.assertIn("[Plan] test", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
