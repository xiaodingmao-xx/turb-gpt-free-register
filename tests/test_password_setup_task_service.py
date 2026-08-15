# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import roxybrowser as roxy_cfg


class PasswordSetupTaskServiceTests(unittest.TestCase):
    def test_password_setup_retry_defaults_are_bounded(self):
        self.assertEqual(roxy_cfg.ROXY_PASSWORD_SETUP_MAX_RETRIES, 3)
        self.assertTrue(roxy_cfg.ROXY_PASSWORD_SETUP_DELETE_TEMP_PROFILE_ON_FAILURE)

    def test_service_exports_account_task_entrypoint(self):
        from core import password_setup_task_service

        self.assertTrue(callable(password_setup_task_service.enqueue_account_password_setup))

    def test_invalid_mode_is_rejected_before_queueing(self):
        from core.password_setup_task_service import validate_password_setup_request

        with self.assertRaises(ValueError):
            validate_password_setup_request("delete_account", "valid-password-123")

    def test_error_redaction_hides_password(self):
        from core.password_setup_task_service import redact_password

        self.assertNotIn("valid-password-123", redact_password("failed valid-password-123", "valid-password-123"))

    def test_enqueue_skips_account_with_saved_registration_password(self):
        from core import db
        from core import password_setup_task_service as service

        account = {
            "id": 7,
            "email": "user@example.com",
            "registration_password": "known-password-123",
        }
        with patch.object(db, "get_account", return_value=account), patch.object(
            db, "claim_account_password_setup"
        ) as claim, patch.object(service, "_EXECUTOR") as executor:
            result = service.enqueue_account_password_setup(
                account_id=7,
                mode="post_login_add_password",
                password="new-password-123",
            )

        self.assertTrue(result["skipped"])
        self.assertTrue(result["already_set"])
        self.assertEqual(result["started_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        claim.assert_not_called()
        executor.submit.assert_not_called()

    def test_password_already_set_result_is_success_without_saving_unknown_password(self):
        from core import db

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 1,
                "email": "user@example.com",
                "extra_json": "{}",
            }]), encoding="utf-8")
            patchers = [
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            ]
            try:
                for item in patchers:
                    item.start()
                self.assertTrue(db.claim_account_password_setup(1))
                self.assertTrue(db.mark_account_password_setup_running(1))
                self.assertTrue(db.update_account_password_setup(1, {
                    "ok": True,
                    "already_set": True,
                    "error": "password_already_set",
                }))
                row = db.get_account(1)
                self.assertEqual(row["password_setup_status"], "already_set")
                self.assertTrue(row["password_setup_ok"])
                self.assertFalse(row.get("registration_password"))
            finally:
                for item in reversed(patchers):
                    item.stop()

    def test_insert_account_promotes_already_set_marker_to_account_status(self):
        from core import db

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text("[]", encoding="utf-8")
            patchers = [
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            ]
            try:
                for item in patchers:
                    item.start()
                account_id = db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    extra={"password_setup_status": "already_set"},
                )
                row = db.get_account(account_id)
                self.assertEqual(row["password_setup_status"], "already_set")
                self.assertTrue(row["password_setup_ok"])
            finally:
                for item in reversed(patchers):
                    item.stop()

    def test_queue_settings_exposes_waiting_count_and_account_positions(self):
        from core import db
        from core import password_setup_task_service as service

        rows = [
            {"id": 11, "password_setup_status": "queued", "password_setup_queued_at": "2026-01-01T00:00:01"},
            {"id": 12, "password_setup_status": "queued", "password_setup_queued_at": "2026-01-01T00:00:02"},
            {"id": 13, "password_setup_status": "running", "password_setup_queued_at": "2026-01-01T00:00:00"},
        ]
        with patch.object(db, "list_accounts", return_value=rows), patch.object(service, "_ACTIVE", {13}):
            snapshot = service.queue_settings()

        self.assertEqual(snapshot["active"], 1)
        self.assertEqual(snapshot["queued"], 2)
        self.assertEqual(snapshot["waiting"], 2)
        self.assertEqual(snapshot["available_workers"], snapshot["workers"] - 1)
        self.assertEqual(snapshot["positions"]["11"], 1)
        self.assertEqual(snapshot["positions"]["12"], 2)

    def test_csrf_abort_diagnostic_is_not_reported_as_roxy_open_failure(self):
        from core.password_setup_task_service import format_password_setup_diagnostic

        class FakeDriver:
            current_url = "https://chatgpt.com/"

        message = format_password_setup_diagnostic(
            RuntimeError("密码设置获取 CSRF 失败: {'error': 'AbortError: signal is aborted without reason', 'stage': 'csrf'}"),
            opened_profile_id="fresh-profile",
            driver=FakeDriver(),
        )

        self.assertIn("stage=csrf", message)
        self.assertIn("AbortError", message)
        self.assertIn("chatgpt.com", message)
        self.assertNotIn("Roxy open diagnostic", message)
        self.assertNotIn("HTTP 502", message)

    def test_password_setup_log_is_written_to_dedicated_file(self):
        from core import password_setup_task_service as service

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(service, "_LOG_DIR", root):
                service._append_password_setup_log("user@example.com", "[设置密码] 已入队", clear=True)
                service._append_password_setup_log("user@example.com", "[设置密码] 失败：示例错误")
                path = service.password_setup_log_path("user@example.com")
                self.assertEqual(path, root / "password-setup-user@example.com.log")
                content = path.read_text(encoding="utf-8")
        self.assertIn("[设置密码] 已入队", content)
        self.assertIn("[设置密码] 失败：示例错误", content)

    def test_password_setup_failure_writes_process_and_error_logs(self):
        from core import db
        from core import password_setup_task_service as service

        with tempfile.TemporaryDirectory() as td:
            with patch.object(service, "_LOG_DIR", Path(td)), patch.object(
                db, "mark_account_password_setup_running", return_value=True
            ), patch.object(
                db, "get_account", return_value={"id": 1, "email": "user@example.com", "extra_json": "{}"}
            ), patch.object(db, "update_account_password_setup", return_value=True):
                result = service._run_password_setup_task(
                    account_id=1,
                    email="user@example.com",
                    mode="post_login_add_password",
                    password="valid-password-123",
                )
            content = (Path(td) / "password-setup-user@example.com.log").read_text(encoding="utf-8")
        self.assertFalse(result["ok"])
        self.assertIn("[设置密码] 开始后台执行", content)
        self.assertIn("[设置密码] 失败", content)

    def test_requeue_account_password_setup_preserves_failure_and_moves_to_queued(self):
        from core import db

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 1,
                "email": "user@example.com",
                "password_setup_status": "running",
                "password_setup_attempt": 1,
            }]), encoding="utf-8")
            patchers = [
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            ]
            try:
                for item in patchers:
                    item.start()
                self.assertTrue(db.requeue_account_password_setup(
                    1,
                    "TimeoutException: page load timeout",
                    attempt=2,
                    max_attempts=3,
                ))
                row = db.get_account(1)
            finally:
                for item in reversed(patchers):
                    item.stop()

        self.assertEqual(row["password_setup_status"], "queued")
        self.assertEqual(row["password_setup_attempt"], 2)
        self.assertEqual(row["password_setup_max_attempts"], 3)
        self.assertEqual(row["password_setup_last_error"], "TimeoutException: page load timeout")
        self.assertIsNone(row.get("password_setup_error"))
        self.assertTrue(row.get("password_setup_queued_at"))

    def test_stale_profile_creates_fresh_environment_and_retries_setup(self):
        from core import db
        from core import password_setup_task_service as service
        from core.roxybrowser_client import RoxyOpenResult

        class FakeDriver:
            def quit(self):
                pass

        class FakeClient:
            def __init__(self):
                self.opened_ids = []
                self.created = 0
                self.cleaned = []

            def open_profile(self, profile_id, *, allow_existing_profile=False):
                self.opened_ids.append((profile_id, allow_existing_profile))
                if profile_id == "stale-profile":
                    raise RuntimeError("Roxy API request failed POST /browser/open HTTP 502: ")
                return RoxyOpenResult(profile_id, {"code": 0})

            def create_profile(self):
                self.created += 1
                return "fresh-profile"

            def cleanup_profile(self, opened):
                self.cleaned.append(opened)

        fake_client = FakeClient()
        account = {
            "id": 1,
            "email": "user@example.com",
            "extra_json": json.dumps({"roxybrowser": {"profile_id": "stale-profile"}}),
        }
        with tempfile.TemporaryDirectory() as td, patch.object(service, "_LOG_DIR", Path(td)), patch.object(
            db, "mark_account_password_setup_running", return_value=True
        ), patch.object(db, "get_account", return_value=account), patch.object(
            db, "update_account_password_setup", return_value=True
        ), patch("core.roxybrowser_client.RoxyBrowserClient", return_value=fake_client), patch(
            "core.roxy_registration._build_driver", return_value=FakeDriver()
        ), patch("core.roxy_registration._run_roxy_password_setup", return_value="saved-pass") as run_setup:
            result = service._run_password_setup_task(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
            )
            content = (Path(td) / "password-setup-user@example.com.log").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(fake_client.created, 1)
        self.assertEqual([item[0] for item in fake_client.opened_ids], ["stale-profile", "fresh-profile"])
        self.assertEqual(len(fake_client.cleaned), 1)
        self.assertTrue(fake_client.cleaned[0].created_by_run)
        run_setup.assert_called_once()
        self.assertIn("fresh-profile", content)

    def test_roxy_missing_window_error_is_treated_as_stale_profile(self):
        from core import password_setup_task_service as service

        error = RuntimeError("Roxy API 返回失败 POST /browser/open: 窗口/数据不存在，请刷新页面后重试")
        self.assertTrue(service._is_stale_profile_open_error(error))

    def test_missing_profile_creates_new_environment_and_retries_setup(self):
        from core import db
        from core import password_setup_task_service as service
        from core.roxybrowser_client import RoxyOpenResult

        class FakeDriver:
            def quit(self):
                pass

        class FakeClient:
            def __init__(self):
                self.created = 0
                self.opened = []
                self.cleaned = []

            def create_profile(self):
                self.created += 1
                return "new-profile"

            def open_profile(self, profile_id, *, allow_existing_profile=False):
                self.opened.append(profile_id)
                if not profile_id:
                    profile_id = self.create_profile()
                return RoxyOpenResult(profile_id, {"code": 0}, created_by_run=True)

            def cleanup_profile(self, opened):
                self.cleaned.append(opened)

        fake_client = FakeClient()
        account = {"id": 1, "email": "user@example.com", "extra_json": "{}"}
        with tempfile.TemporaryDirectory() as td, patch.object(service, "_LOG_DIR", Path(td)), patch.object(
            db, "mark_account_password_setup_running", return_value=True
        ), patch.object(db, "get_account", return_value=account), patch.object(
            db, "update_account_password_setup", return_value=True
        ), patch("core.roxybrowser_client.RoxyBrowserClient", return_value=fake_client), patch(
            "core.roxy_registration._build_driver", return_value=FakeDriver()
        ), patch("core.roxy_registration._run_roxy_password_setup", return_value="saved-pass"):
            result = service._run_password_setup_task(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
            )
            content = (Path(td) / "password-setup-user@example.com.log").read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(fake_client.opened, [""])
        self.assertEqual(fake_client.created, 1)
        self.assertEqual(len(fake_client.cleaned), 1)
        self.assertIn("new-profile", content)

    def test_failed_setup_closes_existing_roxy_environment_without_deleting_it(self):
        from core import db
        from core import password_setup_task_service as service
        from core.roxybrowser_client import RoxyOpenResult

        class FakeDriver:
            def __init__(self):
                self.quit_called = False

            def quit(self):
                self.quit_called = True

        class FakeClient:
            def __init__(self):
                self.cleanup_called = False

            def open_profile(self, profile_id, *, allow_existing_profile=False):
                return RoxyOpenResult(profile_id, {"code": 0})

            def cleanup_profile(self, opened):
                self.cleanup_called = True

        fake_driver = FakeDriver()
        fake_client = FakeClient()
        account = {
            "id": 1,
            "email": "user@example.com",
            "extra_json": json.dumps({"roxybrowser": {"profile_id": "existing-profile"}}),
        }
        with tempfile.TemporaryDirectory() as td, patch.object(service, "_LOG_DIR", Path(td)), patch.object(
            db, "mark_account_password_setup_running", return_value=True
        ), patch.object(db, "get_account", return_value=account), patch.object(
            db, "update_account_password_setup", return_value=True
        ), patch("core.roxybrowser_client.RoxyBrowserClient", return_value=fake_client), patch(
            "core.roxy_registration._build_driver", return_value=fake_driver
        ), patch(
            "core.roxy_registration._run_roxy_password_setup",
            side_effect=RuntimeError("找不到 OTP 输入框"),
        ):
            result = service._run_password_setup_task(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
            )
            content = (Path(td) / "password-setup-user@example.com.log").read_text(encoding="utf-8")

        self.assertFalse(result["ok"])
        self.assertTrue(fake_driver.quit_called)
        self.assertFalse(fake_client.cleanup_called)
        self.assertIn("Roxy", content)
        self.assertNotIn("保留当前 Roxy 窗口", content)

    def test_failed_setup_deletes_new_roxy_environment_after_driver_quit(self):
        from core import db
        from core import password_setup_task_service as service
        from core.roxybrowser_client import RoxyOpenResult

        class FakeDriver:
            def __init__(self):
                self.quit_called = False

            def quit(self):
                self.quit_called = True

        class FakeClient:
            def __init__(self, driver):
                self.driver = driver
                self.cleanup_called = False
                self.driver_quit_before_cleanup = False

            def open_profile(self, profile_id, *, allow_existing_profile=False):
                return RoxyOpenResult("fresh-profile", {"code": 0}, created_by_run=True)

            def cleanup_profile(self, opened):
                self.cleanup_called = True
                self.driver_quit_before_cleanup = self.driver.quit_called

        fake_driver = FakeDriver()
        fake_client = FakeClient(fake_driver)
        account = {"id": 1, "email": "user@example.com", "extra_json": "{}"}
        with tempfile.TemporaryDirectory() as td, patch.object(service, "_LOG_DIR", Path(td)), patch.object(
            db, "mark_account_password_setup_running", return_value=True
        ), patch.object(db, "get_account", return_value=account), patch.object(
            db, "update_account_password_setup", return_value=True
        ), patch("core.roxybrowser_client.RoxyBrowserClient", return_value=fake_client), patch(
            "core.roxy_registration._build_driver", return_value=fake_driver
        ), patch(
            "core.roxy_registration._run_roxy_password_setup",
            side_effect=RuntimeError("TimeoutException: page load timeout"),
        ):
            result = service._run_password_setup_task(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
            )

        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])
        self.assertTrue(fake_driver.quit_called)
        self.assertTrue(fake_client.cleanup_called)
        self.assertTrue(fake_client.driver_quit_before_cleanup)

    def test_retry_classifier_rejects_permanent_and_accepts_transient_errors(self):
        from core.password_setup_task_service import _is_retryable_password_setup_error
        from core.roxy_registration import PasswordAlreadySetError

        self.assertFalse(_is_retryable_password_setup_error(PasswordAlreadySetError("already set")))
        self.assertFalse(_is_retryable_password_setup_error(ValueError("password format invalid")))
        self.assertTrue(_is_retryable_password_setup_error(RuntimeError("TimeoutException: page load timeout")))

    def test_task_wrapper_releases_slot_before_scheduling_retry(self):
        from core import password_setup_task_service as service

        events = []

        class Slot:
            def release(self):
                events.append("release")

        with patch.object(
            service,
            "_run_password_setup_task",
            return_value={
                "ok": False,
                "retryable": True,
                "error": "TimeoutException: page load timeout",
                "attempt": 1,
                "max_attempts": 3,
            },
        ), patch.object(service, "_QUEUE_SLOTS", Slot()), patch.object(
            service,
            "_schedule_password_setup_retry",
            side_effect=lambda **kwargs: events.append("retry"),
        ):
            result = service._run_task_wrapper(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(events, ["release", "retry"])

    def test_retry_schedule_does_not_enqueue_after_max_attempts(self):
        from core import password_setup_task_service as service

        with patch.object(service, "_EXECUTOR") as executor, patch.object(
            service, "_QUEUE_SLOTS"
        ) as slots, patch.object(service, "_append_password_setup_log"):
            scheduled = service._schedule_password_setup_retry(
                account_id=1,
                email="user@example.com",
                mode="post_login_add_password",
                password="valid-password-123",
                result={
                    "ok": False,
                    "retryable": True,
                    "error": "TimeoutException: page load timeout",
                    "attempt": 3,
                    "max_attempts": 3,
                },
            )

        self.assertFalse(scheduled)
        executor.submit.assert_not_called()
        slots.acquire.assert_not_called()

    def test_empty_password_uses_configured_setup_password(self):
        from core.password_setup_task_service import resolve_password_setup_request

        with patch.object(roxy_cfg, "ROXY_PASSWORD_SETUP_PASSWORD", "configured-pass-123"), patch(
            "config.register.REGISTER_PASSWORD", "register-pass-123"
        ):
            mode, password = resolve_password_setup_request("", "")
        self.assertEqual(mode, "post_login_add_password")
        self.assertEqual(password, "configured-pass-123")

    def test_empty_password_falls_back_to_register_password(self):
        from core.password_setup_task_service import resolve_password_setup_request

        with patch.object(roxy_cfg, "ROXY_PASSWORD_SETUP_PASSWORD", ""), patch(
            "config.register.REGISTER_PASSWORD", "register-pass-123"
        ):
            _, password = resolve_password_setup_request("", "")
        self.assertEqual(password, "register-pass-123")

    def test_empty_password_generates_a_valid_password_when_unconfigured(self):
        from core.password_setup_task_service import resolve_password_setup_request

        with patch.object(roxy_cfg, "ROXY_PASSWORD_SETUP_PASSWORD", ""), patch(
            "config.register.REGISTER_PASSWORD", ""
        ):
            _, password = resolve_password_setup_request("", "")
        self.assertGreaterEqual(len(password), 8)
        self.assertLessEqual(len(password), 256)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"\d")
        self.assertRegex(password, r"[^A-Za-z0-9]")

    def test_account_password_setup_status_is_claimed_and_password_is_persisted(self):
        from core import db

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 1,
                "email": "user@example.com",
                "extra_json": json.dumps({"roxybrowser": {"profile_id": "p-1"}}),
            }]), encoding="utf-8")
            patchers = [
                patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
                patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
                patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
                patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
                patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
            ]
            try:
                for item in patchers:
                    item.start()
                self.assertTrue(db.claim_account_password_setup(1))
                self.assertFalse(db.claim_account_password_setup(1))
                self.assertTrue(db.mark_account_password_setup_running(1))
                self.assertTrue(db.update_account_password_setup(1, {"ok": True, "password": "valid-password-123"}))
                row = db.get_account(1)
                self.assertEqual(row["password_setup_status"], "success")
                self.assertTrue(row["registration_password"])
                self.assertIn("valid-password-123", json.loads(row["extra_json"])["registration_password"])
            finally:
                for item in reversed(patchers):
                    item.stop()


if __name__ == "__main__":
    unittest.main()
