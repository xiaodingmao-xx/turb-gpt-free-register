# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db


class AccountTwoFAStateTests(unittest.TestCase):
    def _patch_paths(self, root):
        return ExitStack()

    def test_success_writes_secret_and_busy_tasks_block_claim(self):
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            accounts = root / "accounts.json"
            accounts.write_text(json.dumps([{"id": 1, "email": "user@example.com"}]), encoding="utf-8")
            for name, path in {
                "_ACCOUNTS_JSON": accounts,
                "_LEGACY_ACCOUNTS_JSON": root / "legacy.json",
                "_ACCOUNTS_TXT": root / "accounts.txt",
                "_TOKENS_TXT": root / "tokens.txt",
                "_VIEWER_HTML": root / "viewer.html",
                "_OUTLOOK_JSON": root / "outlook.json",
                "_OUTLOOK_TXT": root / "outlook.txt",
                "_GENERIC_API_EMAIL_JSON": root / "generic.json",
                "_GENERIC_API_EMAIL_TXT": root / "generic.txt",
            }.items():
                stack.enter_context(patch.object(db, name, path))
            self.assertTrue(db.claim_account_twofa_setup(1, max_attempts=3))
            self.assertFalse(db.claim_account_password_setup(1))
            self.assertTrue(db.mark_account_twofa_setup_running(1))
            self.assertTrue(db.update_account_twofa_setup(1, {
                "ok": True, "totp_secret": "JBSWY3DPEHPK3PXP",
            }))
            row = db.get_account(1)
            self.assertEqual(row["twofa_setup_status"], "success")
            self.assertEqual(row["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertIn("JBSWY3DPEHPK3PXP", row["copy_line"])

    def test_failed_result_does_not_write_secret(self):
        with patch.object(db, "_load_accounts", return_value=[{"id": 2, "email": "x@example.com"}]), \
                patch.object(db, "_save_accounts") as save:
            self.assertTrue(db.update_account_twofa_setup(2, {"ok": False, "error": "failed"}))
            saved = save.call_args.args[0][0]
            self.assertNotIn("totp_secret", saved)
            self.assertEqual(saved["twofa_setup_status"], "failed")


if __name__ == "__main__":
    unittest.main()
