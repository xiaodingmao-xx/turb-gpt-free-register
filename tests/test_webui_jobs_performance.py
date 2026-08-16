# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiJobListPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.decorate_retry_info")
    @patch("webui.app.db.list_jobs")
    def test_paged_job_list_uses_one_batch_retry_decoration(
        self, list_jobs, decorate_retry_info
    ):
        rows = [
            {"id": 3, "status": "failed"},
            {"id": 2, "status": "success"},
            {"id": 1, "status": "pending"},
        ]
        list_jobs.return_value = rows
        decorate_retry_info.side_effect = lambda items: [
            dict(item, display_status=item.get("status")) for item in items
        ]

        response = self.client.get("/api/jobs?paged=1&page=1&page_size=2")

        self.assertEqual(response.status_code, 200)
        decorate_retry_info.assert_called_once_with(rows)
        self.assertEqual([item["id"] for item in response.get_json()["items"]], [3, 2])


if __name__ == "__main__":
    unittest.main()
