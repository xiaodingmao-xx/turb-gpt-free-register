# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import roxybrowser as roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient, _saved_proxy_row_to_info


class RoxySavedProxyTests(unittest.TestCase):
    def test_saved_proxy_row_uses_choose_mode_and_proxy_module_id(self):
        row = {
            "id": 42,
            "protocol": "SOCKS5",
            "ipType": "IPV4",
            "lastCountry": "JP",
        }

        self.assertEqual(
            _saved_proxy_row_to_info(row, check_channel="IPRust.io"),
            {
                "moduleId": 42,
                "proxyMethod": "choose",
                "proxyCategory": "SOCKS5",
                "ipType": "IPV4",
                "protocol": "SOCKS5",
                "checkChannel": "IPRust.io",
            },
        )

    def test_saved_proxy_row_requires_a_proxy_id(self):
        with self.assertRaises(ValueError):
            _saved_proxy_row_to_info({"protocol": "SOCKS5"}, check_channel="")

    def test_create_profile_uses_japan_saved_proxy_id(self):
        client = RoxyBrowserClient()
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"code": 0, "data": {"dirId": "profile-1"}}

        rows = [{
            "id": 77,
            "protocol": "SOCKS5",
            "ipType": "IPV4",
            "lastCountry": "JP",
        }]
        with patch.object(roxy_cfg, "ROXY_USE_SAVED_PROXY_POOL", True, create=True), \
                patch.object(roxy_cfg, "ROXY_PROXY_COUNTRY", "JP", create=True), \
                patch.object(roxy_cfg, "ROXY_CREATE_USE_PROXY_POOL", False), \
                patch.object(client, "list_proxies", return_value=rows), \
                patch.object(client, "request", side_effect=request):
            self.assertEqual(client.create_profile(), "profile-1")

        create_body = calls[-1][2]["json_body"]
        self.assertEqual(create_body["proxyInfo"]["moduleId"], 77)
        self.assertEqual(create_body["proxyInfo"]["proxyMethod"], "choose")

    def test_create_profile_rejects_when_country_has_no_saved_proxy(self):
        client = RoxyBrowserClient()
        with patch.object(roxy_cfg, "ROXY_USE_SAVED_PROXY_POOL", True, create=True), \
                patch.object(roxy_cfg, "ROXY_PROXY_COUNTRY", "JP", create=True), \
                patch.object(client, "list_proxies", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "没有找到符合国家筛选"):
                client.create_profile()

    def test_list_proxies_reads_rows_from_roxy_api_response(self):
        client = RoxyBrowserClient()
        rows = [{"id": 1, "lastCountry": "JP"}]
        with patch.object(
            client,
            "request",
            return_value={"code": 0, "data": {"total": 1, "rows": rows}},
        ) as request:
            self.assertEqual(client.list_proxies(page_size=50), rows)

        request.assert_called_once_with(
            "GET",
            "/proxy/list",
            params={"workspaceId": 135422, "page_index": 1, "page_size": 50},
        )


if __name__ == "__main__":
    unittest.main()
