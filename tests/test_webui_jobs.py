# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class WebUiJobFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.get_retry_info", side_effect=lambda row: {"display_status": row.get("status")})
    @patch("webui.app.db.list_jobs")
    def test_jobs_available_filter_returns_success_tasks(self, list_jobs, _retry_info):
        list_jobs.return_value = [
            {"id": 1, "status": "success"},
            {"id": 2, "status": "failed"},
            {"id": 3, "status": "success"},
            {"id": 4, "status": "pending"},
        ]
        response = self.client.get("/api/jobs?paged=1&page=1&page_size=20&status=available")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["items"]], [1, 3])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["all_status_counts"]["active"], 1)

    @patch("webui.app.svc.get_retry_info", side_effect=lambda row: {"display_status": row.get("status")})
    @patch("webui.app.db.list_jobs")
    def test_jobs_failed_filter_returns_only_failed_tasks(self, list_jobs, _retry_info):
        list_jobs.return_value = [
            {"id": 1, "status": "success"},
            {"id": 2, "status": "failed"},
        ]
        response = self.client.get("/api/jobs?paged=1&page=1&page_size=20&status=failed")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["items"]], [2])
        self.assertEqual(payload["total"], 1)

    def test_job_template_has_status_filter(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="jobStatusFilterV2"', html)
        self.assertIn("JOB_STATUS_FILTER", html)
        self.assertIn("status=", html)

    @patch("webui.app.svc.retry_failed_registration_jobs")
    def test_retry_failed_registrations_endpoint_returns_summary(self, retry_failed):
        retry_failed.return_value = {
            "ok": True,
            "found_count": 4,
            "started_count": 2,
            "reused_count": 0,
            "skipped_count": 2,
            "started": [{"job": {"id": 10}}],
            "skipped": [
                {"id": 11, "reason": "账号已存在"},
                {"id": 12, "reason": "账号已存在"},
            ],
            "workers": 2,
        }
        response = self.client.post(
            "/api/jobs/retry-failed-registrations",
            json={"workers": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["started_count"], 2)
        self.assertEqual(response.get_json()["skipped_count"], 2)
        retry_failed.assert_called_once_with(workers=2)

    def test_job_template_has_one_click_failed_registration_button(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="btnRetryFailedRegistrationsV2"', html)
        self.assertIn("retryFailedRegistrations", html)
        self.assertIn("retry-failed-registrations", html)

    @patch("webui.app.db.restore_failed_generic_api_email", return_value=True)
    def test_restore_failed_pool_endpoint_only_returns_restored_items(self, restore):
        response = self.client.post(
            "/api/outlook/restore-failed",
            json={
                "items": [
                    {"email": "failed@example.com", "source": "generic_api"},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["restored_count"], 1)
        self.assertEqual(payload["restored"][0]["email"], "failed@example.com")
        restore.assert_called_once()

    def test_job_template_exposes_failed_pool_restore_action(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn('id="btnRestoreFailedOutlookV2"', html)
        self.assertIn("restore-failed", html)


if __name__ == "__main__":
    unittest.main()
