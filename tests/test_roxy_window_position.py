# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient


class RoxyWindowPositionTests(unittest.TestCase):
    def test_create_profile_requests_first_display_position(self):
        client = RoxyBrowserClient()
        calls = []

        def request(method, path, **kwargs):
            calls.append(kwargs["json_body"])
            return {"code": 0, "data": {"dirId": "profile-1"}}

        with patch.object(client, "request", side_effect=request), \
                patch.object(roxy_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_RANDOM_OS_ON_CREATE", False), \
                patch.object(roxy_cfg, "ROXY_USE_SAVED_PROXY_POOL", False), \
                patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", False), \
                patch.object(roxy_cfg, "ROXY_WORKSPACE_ID", "135422"):
            self.assertEqual(client.create_profile(), "profile-1")

        self.assertTrue(calls[0]["positionSwitch"])
        self.assertEqual(calls[0]["windowRatioPosition"], "0,0")

    def test_open_profile_extracts_roxy_process_id(self):
        client = RoxyBrowserClient()
        response = {
            "code": 0,
            "data": {
                "pid": 24680,
                "http": "127.0.0.1:52314",
                "ws": "ws://127.0.0.1:52314/devtools/browser/test",
            },
        }
        with patch.object(client, "create_profile", return_value="profile-1"), \
                patch.object(client, "request", return_value=response), \
                patch.object(roxy_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True), \
                patch.object(roxy_cfg, "ROXY_PROFILE_ID", ""):
            opened = client.open_profile()

        self.assertEqual(opened.process_id, 24680)


if __name__ == "__main__":
    unittest.main()
