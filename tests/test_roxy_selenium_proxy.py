# -*- coding: utf-8 -*-
import unittest

from selenium.webdriver.chrome.options import Options

from core.roxy_registration import _disable_local_webdriver_proxy


class RoxySeleniumProxyTests(unittest.TestCase):
    def test_local_webdriver_connection_ignores_system_proxy(self):
        options = Options()

        _disable_local_webdriver_proxy(options)

        self.assertTrue(options._ignore_local_proxy)


if __name__ == "__main__":
    unittest.main()
