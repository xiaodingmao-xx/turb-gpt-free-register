# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class WebUiTwoFASetupTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.twofa_task_service.enqueue_account_twofa")
    @patch("webui.app.db.get_account", return_value={"id": 7, "email": "user@example.com"})
    def test_single_account_enqueues_twofa(self, _get, enqueue):
        enqueue.return_value = {
            "accepted": True, "account_id": 7, "email": "user@example.com", "status": "queued",
        }
        response = self.client.post("/api/accounts/7/2fa-setup", json={})
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("totp_secret", response.get_data(as_text=True))

    @patch("webui.app.twofa_task_service.queue_settings", return_value={"active": 0, "queued": 0})
    @patch("webui.app.db.get_account", return_value={
        "id": 7, "email": "user@example.com", "totp_secret": "SECRET",
        "twofa_setup_status": "success",
    })
    def test_status_only_returns_enabled_flag(self, _get, _queue):
        response = self.client.get("/api/accounts/2fa-setup-status?ids=7")
        payload = response.get_json()["items"][0]
        self.assertTrue(payload["totp_enabled"])
        self.assertNotIn("totp_secret", payload)

    def test_template_contains_twofa_actions(self):
        html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("btnTwoFASetupSelectedV2", html)
        self.assertIn("data-account-twofa-setup", html)
        self.assertIn("/api/accounts/2fa-setup-status", html)


if __name__ == "__main__":
    unittest.main()
