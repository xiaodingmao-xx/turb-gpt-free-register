# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import registration_service as service


class RegistrationResourceGuardTests(unittest.TestCase):
    def setUp(self):
        with service._RESOURCE_GUARD_LOCK:
            service._RESOURCE_FAILURE_COUNT = 0
            service._RESOURCE_GUARD_PAUSED = False
            service._RESOURCE_GUARD_REASON = ""
            service._RESOURCE_GUARD_PAUSED_AT = ""

    def test_classify_roxy_capacity_and_memory_as_resource_errors(self):
        self.assertEqual(
            service.classify_registration_error(
                "RuntimeError: Roxy API 返回失败 POST /browser/create: 窗口额度不足"
            ),
            "resource",
        )
        self.assertEqual(
            service.classify_registration_error(
                "RuntimeError: Roxy API 返回失败 POST /browser/open: 内存使用率超过 95%"
            ),
            "resource",
        )

    def test_classify_mailbox_errors_separately(self):
        self.assertEqual(
            service.classify_registration_error(
                "GenericApiMailError: HTTP 404: 最近 25 封邮件中没有找到该邮箱的邮件"
            ),
            "mailbox",
        )
        self.assertEqual(
            service.classify_registration_error("等待通用 API 验证码超时"),
            "mailbox",
        )

    def test_three_resource_errors_pause_new_registration(self):
        for index in range(2):
            status = service.record_resource_failure(f"窗口额度不足 #{index + 1}")
            self.assertFalse(status["paused"])

        status = service.record_resource_failure("内存使用率超过 95%")

        self.assertTrue(status["paused"])
        self.assertEqual(status["failure_count"], 3)
        with self.assertRaises(service.RegistrationResourcePaused):
            service.ensure_registration_allowed()

    def test_success_clears_resource_pause(self):
        service.record_resource_failure("窗口额度不足")
        service.record_resource_failure("窗口额度不足")
        service.record_resource_failure("窗口额度不足")

        status = service.record_registration_success()

        self.assertFalse(status["paused"])
        self.assertEqual(status["failure_count"], 0)
        service.ensure_registration_allowed()

    @patch("core.email_provider.release_email_if_unconsumed")
    def test_resource_release_uses_cooldown_without_disabling_email(self, release):
        service._release_unconsumed_job_email(
            "user@example.com",
            "Roxy API 返回失败: 窗口额度不足",
            cooldown_seconds=600,
        )

        release.assert_called_once_with(
            "user@example.com",
            note="任务未消耗，已自动回收: Roxy API 返回失败: 窗口额度不足",
            cooldown_seconds=600,
        )


if __name__ == "__main__":
    unittest.main()
