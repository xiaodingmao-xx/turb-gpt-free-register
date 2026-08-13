# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest.mock import patch

from core.password_setup_service import PasswordSetupGate
from core.roxy_registration import _run_password_setup_with_gate


class PasswordSetupConcurrencyTests(unittest.TestCase):
    def test_workers_two_allows_two_password_runners_at_once(self):
        gate = PasswordSetupGate(workers=2, queue_limit=10)
        barrier = threading.Barrier(2)
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        results = []

        def runner():
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=2)
            with state_lock:
                active -= 1
            return "ok"

        def worker(email):
            try:
                results.append(gate.run(email, runner))
            except Exception as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"user-{i}@example.com",))
            for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(max_active, 2)

    def test_same_account_cannot_enter_twice(self):
        gate = PasswordSetupGate(workers=2, queue_limit=10)
        started = threading.Event()
        release = threading.Event()
        first_result = []

        def first_runner():
            started.set()
            release.wait(timeout=2)
            return "first"

        first = threading.Thread(
            target=lambda: first_result.append(gate.run("same@example.com", first_runner))
        )
        first.start()
        self.assertTrue(started.wait(timeout=1))

        with self.assertRaisesRegex(RuntimeError, "密码任务已在运行"):
            gate.run("same@example.com", lambda: "second")

        release.set()
        first.join(timeout=2)
        self.assertEqual(first_result, ["first"])

    def test_runner_exception_releases_account_and_worker_slot(self):
        gate = PasswordSetupGate(workers=1, queue_limit=1)

        with self.assertRaisesRegex(ValueError, "boom"):
            gate.run("retry@example.com", lambda: (_ for _ in ()).throw(ValueError("boom")))

        self.assertEqual(gate.run("retry@example.com", lambda: "retry"), "retry")

    def test_roxy_password_setup_is_submitted_through_gate(self):
        driver = object()
        with patch(
            "core.roxy_registration._run_roxy_password_setup",
            return_value="generated-password",
        ) as setup, patch(
            "core.password_setup_service.run_password_setup",
            side_effect=lambda key, runner: (self.assertEqual(key, "user@example.com"), runner())[1],
        ) as gated:
            result = _run_password_setup_with_gate(driver, "user@example.com")

        self.assertEqual(result, "generated-password")
        gated.assert_called_once()
        setup.assert_called_once_with(driver, "user@example.com")


if __name__ == "__main__":
    unittest.main()
