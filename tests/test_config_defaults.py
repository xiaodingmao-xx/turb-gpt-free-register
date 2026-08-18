# -*- coding: utf-8 -*-
import os
import importlib
import unittest
from unittest.mock import patch

from config import env_loader
from webui import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_browser_live_check_defaults_are_safe_and_serial(self):
        from config import roxybrowser

        self.assertEqual(roxybrowser.LIVE_CHECK_BROWSER_WORKERS, 1)
        self.assertEqual(roxybrowser.LIVE_CHECK_BROWSER_QUEUE_LIMIT, 100)
        self.assertEqual(roxybrowser.LIVE_CHECK_BROWSER_MAX_ATTEMPTS, 3)
        self.assertEqual(roxybrowser.LIVE_CHECK_BROWSER_RETRY_DELAYS, "15,60,180")
        self.assertTrue(roxybrowser.LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE)

    def test_config_editor_exposes_browser_live_check_settings(self):
        from webui.config_editor import EDITABLE_FIELDS

        fields = {item["key"]: item for item in EDITABLE_FIELDS}
        expected = {
            "LIVE_CHECK_BROWSER_WORKERS": "int",
            "LIVE_CHECK_BROWSER_QUEUE_LIMIT": "int",
            "LIVE_CHECK_BROWSER_MAX_ATTEMPTS": "int",
            "LIVE_CHECK_BROWSER_RETRY_DELAYS": "str",
            "LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE": "bool",
        }
        self.assertEqual(
            {key: fields[key]["type"] for key in expected},
            expected,
        )

    def test_generic_api_otp_freshness_defaults(self):
        from config import email

        old_loaded = env_loader._LOADED
        try:
            with patch.dict(os.environ, {}, clear=True):
                env_loader._LOADED = True
                reloaded_email = importlib.reload(email)
                self.assertEqual(reloaded_email.OTP_MAX_WAIT, 120)
                self.assertEqual(reloaded_email.OTP_MAX_MESSAGE_AGE_SECONDS, 3600)
                self.assertTrue(reloaded_email.GENERIC_API_REQUIRE_BASELINE)
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(email)

    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_roxy_proxy_country_blank_value_explicitly_disables_country_filter(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"ROXY_PROXY_COUNTRY": "JP"}
        try:
            with patch.dict(os.environ, {"ROXY_PROXY_COUNTRY": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"ROXY_PROXY_COUNTRY": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["ROXY_PROXY_COUNTRY"], "")

    def test_config_editor_preserves_explicit_blank_roxy_proxy_country(self):
        self.assertEqual(
            config_editor._coerce_raw_value(
                "", "JP", "str", key="ROXY_PROXY_COUNTRY"
            ),
            "",
        )

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))


if __name__ == "__main__":
    unittest.main()
