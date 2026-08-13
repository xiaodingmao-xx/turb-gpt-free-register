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
