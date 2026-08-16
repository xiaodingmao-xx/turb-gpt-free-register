# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class WebUiResourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.get_resource_guard_status")
    @patch("webui.app.svc.decorate_retry_info", side_effect=lambda rows: rows)
    @patch("webui.app.db.list_jobs", return_value=[])
    def test_job_list_returns_resource_guard(
        self, _list_jobs, _decorate, guard_status
    ):
        guard_status.return_value = {
            "paused": True,
            "failure_count": 3,
            "threshold": 3,
            "reason": "窗口额度不足",
            "paused_at": "2026-08-16T16:20:00",
        }

        response = self.client.get("/api/jobs?paged=1&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["resource_guard"]["paused"])

    @patch("webui.app.svc.submit_registration")
    def test_job_submit_returns_conflict_when_resource_guard_is_paused(self, submit):
        from core.registration_service import RegistrationResourcePaused

        submit.side_effect = RegistrationResourcePaused("Roxy 资源不足，任务已暂停")

        response = self.client.post(
            "/api/jobs",
            json={"count": 1, "workers": 1},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("资源不足", response.get_json()["error"])

    def test_template_has_resource_guard_and_refresh_lock(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("resource_guard", html)
        self.assertIn("jobsRefreshInFlight", html)
        self.assertIn("}, 5000)", html)


if __name__ == "__main__":
    unittest.main()
