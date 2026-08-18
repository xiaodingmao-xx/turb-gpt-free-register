# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient


class RoxyProfileWindowMigrationTests(unittest.TestCase):
    def test_update_profile_window_position_uses_roxy_mdf_payload(self):
        client = RoxyBrowserClient()
        with patch.object(client, "request", return_value={"code": 0}) as request, \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "135422"):
            result = client.update_profile_window_position("profile-1")

        self.assertEqual(result, {"code": 0})
        request.assert_called_once_with(
            "POST",
            "/browser/mdf",
            params=None,
            json_body={
                "workspaceId": 135422,
                "dirId": "profile-1",
                "positionSwitch": True,
                "windowRatioPosition": "0,0",
            },
        )

    def test_open_existing_profile_enforces_position_only_when_enabled(self):
        client = RoxyBrowserClient()
        response = {"code": 0, "data": {"http": "127.0.0.1:52314"}}
        with patch.object(client, "request", return_value=response), \
                patch.object(client, "update_profile_window_position") as update_position, \
                patch.object(roxy_cfg, "ROXY_PROFILE_ID", "profile-1"), \
                patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", False), \
                patch.object(roxy_cfg, "ROXY_ENFORCE_PRIMARY_WINDOW_POSITION", True):
            client.open_profile()

        update_position.assert_called_once_with("profile-1")

    def test_open_new_profile_confirms_position_before_browser_open(self):
        client = RoxyBrowserClient()
        calls = []
        response = {"code": 0, "data": {"http": "127.0.0.1:52314"}}

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return response

        with patch.object(client, "create_profile", return_value="profile-1"), \
                patch.object(client, "request", side_effect=request), \
                patch.object(client, "update_profile_window_position") as update_position, \
                patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""), \
                patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
                patch.object(roxy_cfg, "ROXY_ENFORCE_PRIMARY_WINDOW_POSITION", True):
            client.open_profile()

        update_position.assert_called_once_with("profile-1")
        self.assertEqual(calls[0][1], "/browser/open")


if __name__ == "__main__":
    unittest.main()
