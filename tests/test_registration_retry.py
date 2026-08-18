# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import registration_service as service


class RegistrationRetryTests(unittest.TestCase):
    @patch.object(service, "retry_job")
    @patch("core.email_provider.check_registration_email_pool", return_value={
        "ok": False,
        "available": 0,
        "sources": ["generic_api"],
        "unknown_sources": [],
    })
    @patch.object(service.db, "list_accounts", return_value=[])
    @patch.object(service.db, "list_jobs")
    def test_one_click_retry_skips_when_source_pool_is_exhausted(
        self, list_jobs, _list_accounts, _pool_check, retry_job
    ):
        list_jobs.return_value = [{
            "id": 1012,
            "status": "failed",
            "email_source": "generic_api",
            "email": None,
        }]

        result = service.retry_failed_registration_jobs(workers=1)

        self.assertEqual(result["started_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["email_pool_exhausted_count"], 1)
        self.assertIn("邮箱池无可用邮箱", result["skipped"][0]["reason"])
        retry_job.assert_not_called()

    @patch.object(service.db, "create_retry_job")
    @patch("core.email_provider.check_registration_email_pool", return_value={
        "ok": False,
        "available": 0,
        "sources": ["generic_api"],
        "unknown_sources": [],
    })
    @patch.object(service, "_account_for_job", return_value=None)
    @patch.object(service, "get_retry_info", return_value={
        "retryable": True,
        "retry_action": "registration",
    })
    @patch.object(service.db, "get_job")
    def test_single_retry_does_not_create_child_job_when_source_pool_is_exhausted(
        self, get_job, _retry_info, _account_for_job, _pool_check, create_retry_job
    ):
        get_job.return_value = {
            "id": 1012,
            "status": "failed",
            "email_source": "generic_api",
        }

        result = service.retry_job(1012, workers=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 409)
        self.assertIn("邮箱池无可用邮箱", result["error"])
        create_retry_job.assert_not_called()

    @patch.object(service, "retry_job")
    @patch("core.email_provider.check_registration_email_pool", return_value={
        "ok": True,
        "available": 1,
        "sources": ["generic_api"],
        "unknown_sources": [],
    })
    @patch.object(service.db, "list_accounts")
    @patch.object(service.db, "list_jobs")
    def test_failed_registration_retry_skips_jobs_already_in_accounts(
        self, list_jobs, list_accounts, _pool_check, retry_job
    ):
        list_jobs.return_value = [
            {"id": 1, "status": "failed", "email": "new@example.com"},
            {"id": 2, "status": "failed", "email": "existing@example.com"},
            {"id": 3, "status": "success", "email": "success@example.com"},
            {"id": 4, "status": "failed", "job_type": "codex_retry", "email": "codex@example.com"},
        ]
        list_accounts.return_value = [{"id": 8, "email": "existing@example.com"}]
        retry_job.return_value = {
            "ok": True,
            "created": True,
            "reused": False,
            "retry_action": "registration",
            "job": {"id": 10},
        }

        result = service.retry_failed_registration_jobs(workers=3)

        self.assertTrue(result["ok"])
        self.assertEqual(result["found_count"], 3)
        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["skipped_count"], 2)
        retry_job.assert_called_once_with(1, workers=3)
        skipped = {item["id"]: item["reason"] for item in result["skipped"]}
        self.assertEqual(skipped[2], "账号已存在")
        self.assertEqual(skipped[4], "不是注册任务")


if __name__ == "__main__":
    unittest.main()
