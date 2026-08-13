# -*- coding: utf-8 -*-
"""Roxy 密码设置并发门控。

每个调用方继续在自己的线程中持有 Selenium driver；本模块只限制同时进入
密码设置阶段的调用数量，不把 driver 转移到线程池，避免 Selenium 跨线程。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar

from config import roxybrowser as roxy_cfg

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _bounded_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(roxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


class PasswordSetupGate:
    """限制密码设置阶段的并发数，并拒绝同一账号重复进入。"""

    def __init__(self, workers: int = 1, queue_limit: int = 100):
        self.workers = max(1, min(16, int(workers)))
        self.queue_limit = max(self.workers, min(5000, int(queue_limit)))
        self._worker_slots = threading.BoundedSemaphore(self.workers)
        self._queue_slots = threading.BoundedSemaphore(self.queue_limit)
        self._keys_lock = threading.RLock()
        self._claimed_keys: set[str] = set()
        self._active = 0

    @staticmethod
    def normalize_key(key: str) -> str:
        value = str(key or "").strip().lower()
        if not value:
            raise ValueError("密码任务缺少账号标识")
        return value

    def try_claim_account(self, key: str) -> bool:
        normalized = self.normalize_key(key)
        with self._keys_lock:
            if normalized in self._claimed_keys:
                return False
            self._claimed_keys.add(normalized)
            return True

    def _release_account(self, key: str) -> None:
        with self._keys_lock:
            self._claimed_keys.discard(key)

    def run(self, key: str, runner: Callable[[], T]) -> T:
        """在并发许可内执行 runner，并在结束后释放账号和队列占位。"""
        normalized = self.normalize_key(key)
        if not self._queue_slots.acquire(blocking=False):
            raise RuntimeError("密码设置队列已满，请稍后重试")

        if not self.try_claim_account(normalized):
            self._queue_slots.release()
            raise RuntimeError(f"密码任务已在运行：{normalized}")

        try:
            logger.info(
                "[密码设置] 等待并发许可：account=%s workers=%s",
                normalized,
                self.workers,
            )
            self._worker_slots.acquire()
            with self._keys_lock:
                self._active += 1
                active = self._active
            logger.info("[密码设置] 获得并发许可：account=%s active=%s/%s", normalized, active, self.workers)
            try:
                return runner()
            finally:
                with self._keys_lock:
                    self._active = max(0, self._active - 1)
                self._worker_slots.release()
        finally:
            self._release_account(normalized)
            self._queue_slots.release()

    def settings(self) -> dict:
        with self._keys_lock:
            active = self._active
            queued = max(0, len(self._claimed_keys) - active)
        return {
            "workers": self.workers,
            "queue_limit": self.queue_limit,
            "active": active,
            "queued": queued,
        }


_GATE = PasswordSetupGate(
    workers=_bounded_int("ROXY_PASSWORD_SETUP_WORKERS", 1, 1, 16),
    queue_limit=_bounded_int("ROXY_PASSWORD_SETUP_QUEUE_LIMIT", 100, 1, 5000),
)


def run_password_setup(key: str, runner: Callable[[], T]) -> T:
    return _GATE.run(key, runner)


def queue_settings() -> dict:
    return _GATE.settings()
