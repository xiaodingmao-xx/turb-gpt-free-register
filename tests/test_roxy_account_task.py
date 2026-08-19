# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.roxy_account_task import profile_id_for_account, open_account_profile_with_recovery


class RoxyAccountTaskTests(unittest.TestCase):
    def test_reads_profile_id_from_extra_json(self):
        self.assertEqual(
            profile_id_for_account({"extra_json": '{"roxybrowser":{"profile_id":"p-1"}}'}),
            "p-1",
        )

    def test_stale_profile_creates_marked_temporary_profile(self):
        client = MagicMock()
        client.open_profile.side_effect = [RuntimeError("HTTP 404 profile not found"), SimpleNamespace(
            profile_id="fresh", created_by_run=False,
        )]
        client.create_profile.return_value = "fresh"
        opened = open_account_profile_with_recovery(client, "old")
        self.assertEqual(opened.profile_id, "fresh")
        self.assertTrue(opened.created_by_run)


if __name__ == "__main__":
    unittest.main()
