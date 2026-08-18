# -*- coding: utf-8 -*-
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from core import registration_service as service


class RegistrationPasswordHandoffTests(unittest.TestCase):
    def _run_successful_handoff_job(self, enqueue_result, account_update_side_effect=None):
        events = []
        account_updates = []

        def update_job(_job_id, **kwargs):
            if kwargs.get("status") == "success":
                events.append("job_success")
            elif kwargs.get("status") == "failed":
                events.append("job_failed")
            return True

        def run_registration(**_kwargs):
            events.append("runner_returned")
            return {
                "success": True,
                "email": "handoff@example.com",
                "account_id": 47,
                "password_setup_handoff": True,
            }

        def enqueue(**_kwargs):
            events.append("enqueue")
            if isinstance(enqueue_result, BaseException):
                raise enqueue_result
            return enqueue_result

        account_update = account_update_side_effect or (
            lambda account_id, result: account_updates.append((account_id, result))
        )
        with patch.object(service, "_activate_job"), patch.object(service, "_deactivate_job"), patch.object(
            service, "_JobLogContext", return_value=nullcontext()
        ), patch.object(service, "_prepare_registration_args", return_value=("handoff@example.com", "Test User", "2000-01-01")), patch.object(
            service, "is_stop_requested", return_value=False
        ), patch.object(service, "check_stop_requested"), patch.object(
            service, "record_registration_success"
        ), patch.object(service.db, "get_job", return_value={"id": 9, "status": "pending"}), patch.object(
            service.db, "update_job", side_effect=update_job
        ), patch.object(
            service.db, "update_account_password_setup", side_effect=account_update
        ), patch(
            "main.run_registration", side_effect=run_registration
        ), patch(
            "core.password_setup_task_service.enqueue_account_password_setup", side_effect=enqueue
        ) as enqueue:
            service._run_one_job(9, "ignored.log")

        return events, account_updates, enqueue

    def test_success_handoff_queues_only_after_runner_returns_and_job_is_successful(self):
        events, account_updates, enqueue = self._run_successful_handoff_job({"accepted": True})

        self.assertEqual(events, ["runner_returned", "job_success", "enqueue"])
        self.assertEqual(account_updates, [])
        self.assertEqual(
            enqueue.call_args.kwargs,
            {"account_id": 47, "mode": "", "password": "", "trigger": "registration_handoff"},
        )

    def test_queue_rejection_marks_password_setup_failed_without_failing_registration_job(self):
        events, account_updates, _enqueue = self._run_successful_handoff_job(
            {"accepted": False, "error": "设置密码队列已满"}
        )

        self.assertEqual(events, ["runner_returned", "job_success", "enqueue"])
        self.assertEqual(account_updates, [(47, {"ok": False, "error": "设置密码队列已满"})])
        self.assertNotIn("job_failed", events)

    def test_enqueue_exception_marks_password_setup_failed_without_failing_registration_job(self):
        events, account_updates, _enqueue = self._run_successful_handoff_job(
            RuntimeError("queue unavailable")
        )

        self.assertEqual(events, ["runner_returned", "job_success", "enqueue"])
        self.assertEqual(account_updates, [(47, {"ok": False, "error": "RuntimeError: queue unavailable"})])
        self.assertNotIn("job_failed", events)

    def test_account_status_write_exception_keeps_registration_job_successful(self):
        events, _account_updates, _enqueue = self._run_successful_handoff_job(
            {"accepted": False, "error": "设置密码队列已满"},
            account_update_side_effect=RuntimeError("database unavailable"),
        )

        self.assertEqual(events, ["runner_returned", "job_success", "enqueue"])
        self.assertNotIn("job_failed", events)

    def test_malformed_enqueue_responses_are_rejected_without_failing_registration_job(self):
        for response, expected_error in (
            ("unexpected response", "设置密码入队返回无效结果"),
            ({"accepted": "false", "error": "设置密码队列已满"}, "设置密码队列已满"),
        ):
            with self.subTest(response=response):
                events, account_updates, _enqueue = self._run_successful_handoff_job(response)

                self.assertEqual(events, ["runner_returned", "job_success", "enqueue"])
                self.assertEqual(account_updates, [(47, {"ok": False, "error": expected_error})])
                self.assertNotIn("job_failed", events)

    def test_queue_failure_log_does_not_expose_password_or_otp(self):
        sensitive_error = "setup failed password=very-secret otp=123456"

        with self.assertLogs(service.logger, level="WARNING") as logs:
            self._run_successful_handoff_job({"accepted": False, "error": sensitive_error})

        rendered = "\n".join(logs.output)
        self.assertIn("password_setup=queue_failed", rendered)
        self.assertNotIn("very-secret", rendered)
        self.assertNotIn("123456", rendered)

    def test_invalid_handoff_account_ids_do_not_enqueue_or_update_accounts(self):
        invalid_ids = (True, 0, -1, " 47", "47.0", 47.0, "account-47")
        for account_id in invalid_ids:
            with self.subTest(account_id=account_id), patch(
                "core.password_setup_task_service.enqueue_account_password_setup"
            ) as enqueue, patch.object(service.db, "update_account_password_setup") as update_account:
                result = service._enqueue_password_setup_handoff(
                    {"success": True, "password_setup_handoff": True, "account_id": account_id}
                )

                self.assertIsNone(result)
                enqueue.assert_not_called()
                update_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
