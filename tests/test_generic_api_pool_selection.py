# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db, email_provider


class GenericApiPoolSelectionTests(unittest.TestCase):
    @patch("core.db._save_generic_api_emails")
    @patch("core.db._load_generic_api_emails")
    def test_restore_failed_email_makes_only_failed_row_available(self, load, save):
        rows = [
            {"id": 1, "email": "failed@example.com", "status": "failed", "used_at": "old", "cooldown_until": "old"},
            {"id": 2, "email": "used@example.com", "status": "used", "used_at": "old"},
        ]
        load.return_value = rows

        changed = db.restore_failed_generic_api_email(
            "failed@example.com", note="用户手动恢复失败邮箱"
        )

        self.assertTrue(changed)
        self.assertEqual(rows[0]["status"], "available")
        self.assertIsNone(rows[0]["used_at"])
        self.assertIsNone(rows[0]["cooldown_until"])
        self.assertEqual(rows[0]["note"], "用户手动恢复失败邮箱")
        self.assertEqual(rows[1]["status"], "used")
        save.assert_called_once_with(rows)

    @patch("core.db._save_generic_api_emails")
    @patch("core.db._load_generic_api_emails")
    def test_restore_failed_email_does_not_change_non_failed_row(self, load, save):
        rows = [{"id": 1, "email": "available@example.com", "status": "available"}]
        load.return_value = rows

        changed = db.restore_failed_generic_api_email("available@example.com")

        self.assertFalse(changed)
        self.assertEqual(rows[0]["status"], "available")
        save.assert_not_called()

    @patch("core.db._save_generic_api_emails")
    @patch("core.db._load_generic_api_emails")
    @patch("core.db._now", return_value="2026-08-16T16:00:00")
    def test_claim_skips_email_in_cooldown(self, _now, load, save):
        rows = [
            {
                "id": 1,
                "email": "first@example.com",
                "status": "available",
                "cooldown_until": "2026-08-16T16:10:00",
            },
            {"id": 2, "email": "second@example.com", "status": "available"},
        ]
        load.return_value = rows

        selected = db.claim_next_generic_api_email()

        self.assertEqual(selected["email"], "second@example.com")
        self.assertEqual(rows[1]["status"], "used")
        save.assert_called_once_with(rows)

    @patch("core.db._save_generic_api_emails")
    @patch("core.db._load_generic_api_emails")
    @patch("core.db._load_accounts", return_value=[])
    @patch("core.db._now", return_value="2026-08-16T16:00:00")
    def test_release_unconsumed_email_adds_cooldown(
        self, _now, _accounts, load, save
    ):
        rows = [{"id": 1, "email": "first@example.com", "status": "used"}]
        load.return_value = rows

        changed = db.release_unconsumed_generic_api_email(
            "first@example.com",
            note="Roxy 窗口额度不足",
            cooldown_seconds=600,
        )

        self.assertTrue(changed)
        self.assertEqual(rows[0]["status"], "available")
        self.assertEqual(rows[0]["cooldown_until"], "2026-08-16T16:10:00")
        self.assertEqual(rows[0]["note"], "Roxy 窗口额度不足")
        save.assert_called_once_with(rows)

    @patch("core.db._save_generic_api_emails")
    @patch("core.db._load_generic_api_emails")
    @patch("core.db._now", return_value="2026-08-16T16:11:00")
    def test_claim_can_use_email_after_cooldown_expires(self, _now, load, save):
        rows = [
            {
                "id": 1,
                "email": "first@example.com",
                "status": "available",
                "cooldown_until": "2026-08-16T16:10:00",
            }
        ]
        load.return_value = rows

        selected = db.claim_next_generic_api_email()

        self.assertEqual(selected["email"], "first@example.com")
        self.assertEqual(rows[0]["status"], "used")
        self.assertIsNone(rows[0].get("cooldown_until"))
        save.assert_called_once_with(rows)

    @patch("core.db.release_unconsumed_generic_api_email", return_value=True)
    @patch("core.email_provider.resolve_email_source", return_value="generic_api")
    def test_provider_passes_cooldown_to_generic_api_pool(self, _resolve, release):
        changed = email_provider.release_email_if_unconsumed(
            "first@example.com",
            note="Roxy 窗口额度不足",
            cooldown_seconds=600,
        )

        self.assertTrue(changed)
        release.assert_called_once_with(
            "first@example.com",
            note="Roxy 窗口额度不足",
            cooldown_seconds=600,
        )


if __name__ == "__main__":
    unittest.main()
