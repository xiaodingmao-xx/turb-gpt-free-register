# -*- coding: utf-8 -*-
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from webui.app import create_app


class WebUiAccountFeatureTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.list_accounts_page")
    def test_account_list_exposes_region_and_payment_detection_fields(self, list_page):
        list_page.return_value = {
            "items": [{
                "id": 7,
                "email": "user@example.com",
                "plan_eligibility_country": "JP",
                "plan_exit_country": "JP",
                "plan_exit_city": "Tokyo",
                "extract_link_payment_detected": True,
                "extract_link_payment_session_kind": "oaics",
                "extract_link_payment_session_id": "oaics_test123",
            }],
            "total": 1,
            "sources": [],
            "revision": "1:now",
        }
        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")
        self.assertEqual(response.status_code, 200)
        row = response.get_json()["items"][0]
        self.assertEqual(row["plan_eligibility_country"], "JP")
        self.assertEqual(row["extract_link_payment_session_kind"], "oaics")

    def test_account_template_mentions_region_and_session_labels(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("\\u8d44\\u683c\\u5730\\u533a", html)
        self.assertIn("\\u51fa\\u53e3\\u5730\\u533a", html)
        self.assertIn("OAICS", html)
        self.assertIn("Stripe cs_", html)

    def test_account_template_hides_password_setup_for_completed_accounts(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("'already_set'", html)
        self.assertIn("passwordDone", html)

    def test_account_template_displays_password_queue_position_and_summary(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("password_setup_queue_position", html)
        self.assertIn("passwordSetupQueueStatusV2", html)
        self.assertIn("renderPasswordSetupQueueStatus", html)

    @patch("webui.app.password_setup_task_service.queue_settings")
    @patch("webui.app.db.list_accounts_page")
    def test_account_list_exposes_password_queue_snapshot_and_position(self, list_page, queue_settings):
        list_page.return_value = {
            "items": [{"id": 7, "email": "user@example.com", "password_setup_status": "queued"}],
            "total": 1,
            "sources": [],
            "revision": "1:now",
        }
        queue_settings.return_value = {
            "workers": 1, "active": 1, "queued": 1, "waiting": 1,
            "available_workers": 0, "positions": {"7": 1},
        }
        response = self.client.get("/api/accounts?paged=1&page=1&page_size=20")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["items"][0]["password_setup_queue_position"], 1)
        self.assertEqual(payload["password_setup_queue"]["waiting"], 1)

    @patch("webui.app.password_setup_task_service.enqueue_account_password_setup")
    @patch("webui.app.db.get_account")
    def test_single_password_setup_returns_accepted_without_plaintext(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "user@example.com"}
        enqueue.return_value = {
            "accepted": True,
            "account_id": 7,
            "email": "user@example.com",
            "status": "queued",
            "future": object(),
        }
        response = self.client.post(
            "/api/accounts/7/password-setup",
            json={"mode": "post_login_add_password", "password": "valid-password-123"},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertNotIn("valid-password-123", response.get_data(as_text=True))
        self.assertEqual(payload["status"], "queued")
        enqueue.assert_called_once_with(
            account_id=7,
            mode="post_login_add_password",
            password="valid-password-123",
            trigger="manual",
        )

    @patch("webui.app.password_setup_task_service.enqueue_account_password_setup")
    def test_invalid_password_setup_mode_returns_bad_request(self, enqueue):
        enqueue.side_effect = ValueError("设置密码模式无效")
        response = self.client.post(
            "/api/accounts/7/password-setup",
            json={"mode": "delete_account", "password": "valid-password-123"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    @patch("webui.app.password_setup_task_service.enqueue_account_password_setup")
    @patch("webui.app.db.get_account")
    def test_password_setup_can_resolve_password_server_side(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "user@example.com"}
        enqueue.return_value = {
            "accepted": True,
            "account_id": 7,
            "email": "user@example.com",
            "status": "queued",
            "future": object(),
        }
        response = self.client.post("/api/accounts/7/password-setup", json={})
        self.assertEqual(response.status_code, 202)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["password"], "")
        self.assertEqual(kwargs["mode"], "post_login_add_password")

    @patch("webui.app.password_setup_task_service.enqueue_account_password_setup")
    @patch("webui.app.db.get_account")
    def test_single_password_setup_returns_skipped_when_password_already_set(self, get_account, enqueue):
        get_account.return_value = {"id": 7, "email": "user@example.com", "registration_password": "known-pass"}
        enqueue.return_value = {
            "accepted": False,
            "skipped": True,
            "already_set": True,
            "account_id": 7,
            "email": "user@example.com",
            "status": "success",
            "started_count": 0,
            "skipped_count": 1,
            "error": "账号密码已经设置，已跳过设置密码任务",
        }
        response = self.client.post("/api/accounts/7/password-setup", json={})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["skipped_count"], 1)

    @patch("webui.app.password_setup_task_service.enqueue_account_password_setup")
    @patch("webui.app.db.get_account")
    def test_bulk_password_setup_returns_per_account_results(self, get_account, enqueue):
        get_account.side_effect = lambda account_id: {"id": int(account_id), "email": f"{account_id}@example.com"}
        enqueue.side_effect = lambda **kwargs: {
            "accepted": True,
            "account_id": kwargs["account_id"],
            "email": f"{kwargs['account_id']}@example.com",
            "status": "queued",
            "future": object(),
        }
        response = self.client.post(
            "/api/accounts/password-setup-bulk",
            json={"account_ids": [1, 2], "mode": "post_login_password_reset", "password": "valid-password-123"},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["started_count"], 2)
        self.assertNotIn("valid-password-123", response.get_data(as_text=True))

    @patch("webui.app.db.list_accounts_page")
    def test_account_query_forwards_filters_and_sort_to_database(self, list_page):
        list_page.return_value = {"items": [], "total": 0, "sources": [], "revision": "0:"}
        response = self.client.get(
            "/api/accounts?paged=1&page=2&page_size=10&email=alpha&source=outlook&sort=created_at&order=asc"
        )
        self.assertEqual(response.status_code, 200)
        kwargs = list_page.call_args.kwargs
        self.assertEqual(kwargs["email_filter"], "alpha")
        self.assertEqual(kwargs["source_filter"], "outlook")
        self.assertEqual(kwargs["sort_key"], "created_at")
        self.assertEqual(kwargs["sort_order"], "asc")

    def test_password_setup_is_direct_and_has_no_password_modal(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertNotIn('id="passwordSetupModal"', html)
        start = html.index("function setPasswordForSelectedAccounts() {")
        end = html.index("function toggleAccountSort", start)
        direct_action = html[start:end]
        self.assertIn("submitPasswordSetupForIds(Array.from(ACCOUNT_SELECTED)", direct_action)
        self.assertNotIn("openPasswordSetupModal", direct_action)
        self.assertIn("openPasswordSetupLog", html)
        self.assertIn("password-setup-log", html)

    def test_legacy_account_template_exposes_password_setup_log(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index_legacy.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("data-account-password-log", html)
        self.assertIn("password-setup-log", html)

    @patch("webui.app.password_setup_task_service.password_setup_log_path")
    @patch("webui.app.db.get_account_by_email")
    def test_password_setup_log_endpoint_returns_log_and_running_state(self, get_account, log_path):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "password-setup-user.log"
            path.write_text("13:00:00 [INFO] [设置密码] 已入队\n", encoding="utf-8")
            log_path.return_value = path
            get_account.return_value = {"id": 7, "email": "user@example.com", "password_setup_status": "running"}
            response = self.client.get("/api/accounts/password-setup-log?email=user%40example.com")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["running"])
        self.assertIn("[设置密码] 已入队", payload["log"])

    def test_password_setup_request_does_not_send_plaintext_password_from_browser(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        start = html.index("async function submitPasswordSetupForIds")
        end = html.index("async function submitPasswordSetup()", start)
        submit_function = html[start:end]
        self.assertIn("body: JSON.stringify(body)", submit_function)
        self.assertIn("{account_ids: normalized}", submit_function)
        self.assertNotIn("passwordSetupValueV2", submit_function)

    def test_password_setup_polling_keeps_submitted_ids_after_account_reload(self):
        template = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        html = template.read_text(encoding="utf-8")
        self.assertIn("let accountPasswordSetupPollingIds = new Set();", html)

        submit_start = html.index("async function submitPasswordSetupForIds")
        submit_end = html.index("async function pollAccountPasswordSetupStatuses()", submit_start)
        submit_function = html[submit_start:submit_end]
        self.assertIn("startAccountPasswordSetupPolling(normalized);", submit_function)

        poll_start = html.index("async function pollAccountPasswordSetupStatuses()")
        poll_end = html.index("function setPasswordForSelectedAccounts()", poll_start)
        poll_functions = html[poll_start:poll_end]
        self.assertIn("Array.from(accountPasswordSetupPollingIds)", poll_functions)
        self.assertIn("accountPasswordSetupPollingIds.delete", poll_functions)


if __name__ == "__main__":
    unittest.main()
