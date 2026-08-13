# -*- coding: utf-8 -*-
import unittest


class ProxyNormalizationTests(unittest.TestCase):
    def test_legacy_host_port_user_password_is_converted_to_socks_url(self):
        from core.proxy_utils import normalize_proxy_url

        self.assertEqual(
            normalize_proxy_url("proxy.example:1000:user-name:pass word"),
            "socks5h://user-name:pass%20word@proxy.example:1000",
        )

    def test_colon_in_legacy_password_is_preserved(self):
        from core.proxy_utils import normalize_proxy_url

        self.assertEqual(
            normalize_proxy_url("proxy.example:1000:user:part:two"),
            "socks5h://user:part%3Atwo@proxy.example:1000",
        )

    def test_existing_proxy_url_is_not_changed(self):
        from core.proxy_utils import normalize_proxy_url

        value = "socks5h://user:pass@proxy.example:1000"
        self.assertEqual(normalize_proxy_url(value), value)

    def test_plan_check_route_passes_normalized_proxy_to_session(self):
        from core.chatgpt_plan import resolve_plan_check_route

        route = resolve_plan_check_route("proxy.example:1000:user:pass")
        self.assertEqual(route["proxy"], "socks5h://user:pass@proxy.example:1000")
        self.assertEqual(route["network_route"], "proxy")


if __name__ == "__main__":
    unittest.main()
