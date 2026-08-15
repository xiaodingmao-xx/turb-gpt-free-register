# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountListQueryTests(unittest.TestCase):
    def _db_paths(self, root: Path):
        return [
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy_accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]

    def test_filters_email_and_source_before_pagination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": 1, "email": "alpha@example.com", "email_source": "outlook", "created_at": "2026-08-01T10:00:00"},
                {"id": 2, "email": "beta@example.com", "email_source": "icloud", "created_at": "2026-08-02T10:00:00"},
                {"id": 3, "email": "alpha-2@example.com", "email_source": "outlook", "created_at": "2026-08-03T10:00:00"},
            ]
            (root / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                result = db.list_accounts_page(
                    limit=1,
                    offset=0,
                    email_filter="alpha",
                    source_filter="outlook",
                    sort_key="created_at",
                    sort_order="asc",
                )
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["items"][0]["email"], "alpha@example.com")

    def test_created_at_sort_is_stable_and_supports_descending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": 1, "email": "one@example.com", "created_at": "2026-08-01T10:00:00"},
                {"id": 2, "email": "two@example.com", "created_at": "2026-08-03T10:00:00"},
                {"id": 3, "email": "three@example.com", "created_at": "2026-08-02T10:00:00"},
            ]
            (root / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                result = db.list_accounts_page(sort_key="created_at", sort_order="desc")
            self.assertEqual([row["id"] for row in result["items"]], [2, 3, 1])

    def test_unknown_sort_key_falls_back_to_id_without_field_access(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {"id": 2, "email": "two@example.com"},
                {"id": 1, "email": "one@example.com"},
            ]
            (root / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                result = db.list_accounts_page(sort_key="__class__", sort_order="asc")
            self.assertEqual([row["id"] for row in result["items"]], [1, 2])

    def test_dead_account_candidate_requires_explicit_deactivated_status(self):
        self.assertTrue(db.is_dead_account_candidate({"live_check_status": "deactivated"}))
        self.assertTrue(db.is_dead_account_candidate({"codex_status": "deactivated"}))
        self.assertFalse(db.is_dead_account_candidate({
            "live_check_status": "failed",
            "live_check_error": "ProxyError: Received invalid version in initial SOCKS5 response",
        }))
        self.assertFalse(db.is_dead_account_candidate({"plan_check_status": "failed"}))

    def test_archive_dead_accounts_revalidates_and_preserves_account_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [
                {
                    "id": 1,
                    "email": "dead@example.com",
                    "access_token": "keep-token",
                    "live_check_status": "deactivated",
                },
                {
                    "id": 2,
                    "email": "proxy@example.com",
                    "access_token": "proxy-token",
                    "live_check_status": "failed",
                    "live_check_error": "ProxyError",
                },
                {
                    "id": 3,
                    "email": "running@example.com",
                    "access_token": "running-token",
                    "codex_status": "deactivated",
                    "password_setup_status": "running",
                },
            ]
            (root / "accounts.json").write_text(json.dumps(rows), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                archived, skipped = db.archive_dead_accounts([1, 2, 3])
                rows_after = {row["id"]: row for row in db.list_accounts(limit=20, archived="all")}

            self.assertEqual([row["id"] for row in archived], [1])
            self.assertEqual(rows_after[1]["access_token"], "keep-token")
            self.assertTrue(rows_after[1]["archived"])
            self.assertEqual(rows_after[1]["archived_reason"], "dead_account_bulk")
            self.assertFalse(rows_after[2].get("archived", False))
            self.assertFalse(rows_after[3].get("archived", False))
            self.assertTrue(any(item["id"] == 2 for item in skipped))
            self.assertTrue(any(item["id"] == 3 for item in skipped))

            with self._stack(*self._db_paths(root)):
                db.archive_accounts([1], archived=False)
                restored = db.get_account(1)
            self.assertFalse(restored["archived"])
            self.assertIsNone(restored.get("archived_reason"))
            self.assertIsNone(restored.get("archived_source"))

    def test_archived_account_cannot_claim_background_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([{
                "id": 1,
                "email": "archived@example.com",
                "archived": True,
            }]), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                self.assertFalse(db.claim_account_password_setup(1))
                self.assertFalse(db.claim_account_live_check(1))
                self.assertFalse(db.claim_account_plan_check(acc_id=1))
                self.assertFalse(db.claim_account_extract(1))
                self.assertFalse(db.claim_account_codex_agent(1))

    def test_recover_interrupted_password_setup_marks_queued_and_running_as_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([
                {"id": 1, "email": "queued@example.com", "password_setup_status": "queued"},
                {"id": 2, "email": "running@example.com", "password_setup_status": "running"},
                {"id": 3, "email": "done@example.com", "password_setup_status": "success"},
            ]), encoding="utf-8")
            with self._stack(*self._db_paths(root)):
                recovered = db.recover_interrupted_password_setups()
                rows = {row["id"]: row for row in db.list_accounts(limit=20, archived="all")}

            self.assertEqual(recovered, 2)
            self.assertEqual(rows[1]["password_setup_status"], "failed")
            self.assertEqual(rows[2]["password_setup_status"], "failed")
            self.assertIn("WebUI 重启", rows[1]["password_setup_error"])
            self.assertEqual(rows[3]["password_setup_status"], "success")

    @staticmethod
    def _stack(*patchers):
        class _Stack:
            def __enter__(self):
                self.values = [p.start() for p in patchers]
                return self.values

            def __exit__(self, exc_type, exc, tb):
                for p in reversed(patchers):
                    p.stop()
                return False

        return _Stack()


if __name__ == "__main__":
    unittest.main()
