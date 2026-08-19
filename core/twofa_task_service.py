# -*- coding: utf-8 -*-
"""已有账号手动补设 2FA 后台任务。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from config import roxybrowser as roxy_cfg
from config import twofa as twofa_cfg
from core import db
from core.roxy_twofa import (
    TwoFAEnrollmentUncertain,
    redact_twofa_error,
    setup_existing_account_2fa,
)
from core.roxy_account_task import profile_id_for_account, open_account_profile_with_recovery

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def twofa_setup_log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"twofa-setup-{safe}.log"


def _append_log(email: str, message: str, *, level: str = "INFO", clear: bool = False) -> None:
    try:
        path = twofa_setup_log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if clear else "a"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(
                f"{datetime.now().strftime('%H:%M:%S')} [{level}] "
                f"{redact_twofa_error(message)}\n"
            )
    except Exception:
        logger.exception("2FA task log write failed email=%s", email)


def _bounded(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(twofa_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, TwoFAEnrollmentUncertain):
        return False
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        return bool(retryable)
    text = str(exc or "").lower()
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(x in text for x in (
        "timeout", "超时", "connection", "network", "roxy api", "otp", "验证码", "http 5",
    ))


def _run_twofa_task(*, account_id: int, email: str) -> dict:
    from core.roxy_registration import _build_driver, login_existing_account_with_otp
    from core.roxybrowser_client import RoxyBrowserClient

    client = None
    opened = None
    driver = None
    attempt = 1
    max_attempts = _bounded("TWOFA_SETUP_MAX_ATTEMPTS", 3, 1, 10)
    try:
        if not db.mark_account_twofa_setup_running(account_id):
            raise RuntimeError("2FA 任务状态已失效")
        account = db.get_account(account_id) or {}
        attempt = max(1, int(account.get("twofa_setup_attempt") or 1))
        max_attempts = max(1, int(account.get("twofa_setup_max_attempts") or max_attempts))
        profile_id = profile_id_for_account(account)
        _append_log(email, f"[2FA] 开始 account_id={account_id} attempt={attempt}/{max_attempts}")
        client = RoxyBrowserClient()
        opened = open_account_profile_with_recovery(
            client,
            profile_id,
            progress_callback=lambda message, level="INFO": _append_log(
                email, f"[2FA] {message}", level=level,
            ),
        )
        driver = _build_driver(opened)

        db.update_account_twofa_setup_phase(account_id, "login")
        session = login_existing_account_with_otp(
            driver,
            email,
            progress_callback=lambda message: _append_log(email, message),
        )
        user = session.get("user") if isinstance(session, dict) else {}
        actual_email = str((user or {}).get("email") or "").strip().lower()
        expected_email = str(email or "").strip().lower()
        if not actual_email:
            raise ValueError("2FA 登录态缺少账号邮箱，已拒绝继续")
        if actual_email != expected_email:
            raise ValueError("Roxy Profile 登录账号与目标账号不一致，已拒绝继续")
        if bool((user or {}).get("mfa")):
            result = {
                "ok": False,
                "already_enabled_external": True,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            db.update_account_twofa_setup(account_id, result)
            _append_log(email, "[2FA] 平台已启用 MFA，但本地没有可恢复的 Secret", level="WARNING")
            return result

        db.update_account_twofa_setup_phase(account_id, "reauth")

        def progress(message: str) -> None:
            text = str(message or "")
            if "创建 TOTP" in text:
                db.update_account_twofa_setup_phase(account_id, "enroll")
            elif "激活 TOTP" in text:
                db.update_account_twofa_setup_phase(account_id, "activate")
            _append_log(email, text)

        result = setup_existing_account_2fa(
            driver,
            email,
            progress_callback=progress,
        )
        db.update_account_twofa_setup(account_id, result)
        _append_log(email, "[2FA] 补设完成，Secret 已安全写回")
        return result
    except Exception as exc:
        safe_error = redact_twofa_error(f"{type(exc).__name__}: {exc}")
        result = {
            "ok": False,
            "retryable": _retryable(exc),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error": safe_error,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_twofa_setup(account_id, result)
        _append_log(email, f"[2FA] 失败：{safe_error}", level="ERROR")
        return result
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logger.exception("关闭 2FA Roxy driver 失败")
        if client is not None and opened is not None:
            try:
                client.cleanup_profile(opened)
            except Exception:
                logger.exception("清理 2FA Roxy Profile 失败 profile_id=%s", getattr(opened, "profile_id", ""))


_WORKERS = _bounded("TWOFA_SETUP_WORKERS", 1, 1, 8)
_QUEUE_LIMIT = _bounded("TWOFA_SETUP_QUEUE_LIMIT", 100, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="twofa-setup")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_LOCK = threading.Lock()
_ACTIVE: set[int] = set()


def enqueue_account_twofa(*, account_id: int, trigger: str = "manual") -> dict:
    account = db.get_account(int(account_id))
    if not account:
        return {"accepted": False, "error": "账号不存在"}
    email = str(account.get("email") or "").strip()
    if not email:
        return {"accepted": False, "error": "账号邮箱为空"}
    if str(account.get("totp_secret") or "").strip():
        return {
            "accepted": False, "skipped": True, "already_set": True,
            "account_id": int(account_id), "email": email, "error": "账号已保存 2FA Secret",
        }
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "queue_full": True, "error": "2FA 队列已满，请稍后重试"}
    max_attempts = _bounded("TWOFA_SETUP_MAX_ATTEMPTS", 3, 1, 10)
    if not db.claim_account_twofa_setup(
        int(account_id), trigger=trigger, max_attempts=max_attempts,
    ):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号存在冲突任务或不可补设 2FA"}
    _append_log(email, f"[2FA] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        future = _EXECUTOR.submit(_run_wrapper, account_id=int(account_id), email=email)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        db.update_account_twofa_setup(account_id, {"ok": False, "error": "2FA 任务入队失败"})
        return {"accepted": False, "error": redact_twofa_error(exc)}
    return {
        "accepted": True, "account_id": int(account_id), "email": email,
        "status": "queued", "trigger": trigger, "future": future,
    }


def _run_wrapper(*, account_id: int, email: str) -> dict:
    with _LOCK:
        _ACTIVE.add(int(account_id))
    result = {}
    try:
        result = _run_twofa_task(account_id=account_id, email=email)
        return result
    finally:
        with _LOCK:
            _ACTIVE.discard(int(account_id))
        _QUEUE_SLOTS.release()
        if result.get("retryable"):
            _schedule_retry(account_id=account_id, email=email, result=result)


def _schedule_retry(*, account_id: int, email: str, result: dict) -> bool:
    attempt = max(1, int(result.get("attempt") or 1))
    max_attempts = max(1, int(result.get("max_attempts") or 1))
    if attempt >= max_attempts:
        return False
    delay = 15 if attempt == 1 else (60 if attempt == 2 else 180)
    retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    if not db.requeue_account_twofa_setup(
        account_id, str(result.get("error") or "2FA 任务失败"),
        attempt=attempt + 1, max_attempts=max_attempts, next_retry_at=retry_at,
    ):
        return False

    def submit() -> None:
        if not _QUEUE_SLOTS.acquire(blocking=False):
            timer = threading.Timer(5, submit)
            timer.daemon = True
            timer.start()
            return
        db.requeue_account_twofa_setup(
            account_id, str(result.get("error") or "2FA 任务失败"),
            attempt=attempt + 1, max_attempts=max_attempts, next_retry_at=None,
        )
        try:
            _EXECUTOR.submit(_run_wrapper, account_id=account_id, email=email)
        except Exception:
            _QUEUE_SLOTS.release()
            db.update_account_twofa_setup(account_id, {"ok": False, "error": "2FA 自动重试入队失败"})

    timer = threading.Timer(delay, submit)
    timer.daemon = True
    timer.start()
    _append_log(email, f"[2FA] 已安排自动重试 attempt={attempt + 1}/{max_attempts} delay={delay}", level="WARNING")
    return True


def queue_settings() -> dict:
    with _LOCK:
        active = len(_ACTIVE)
    rows = db.list_accounts(limit=5000, archived="all")
    queued = sum(1 for row in rows if str(row.get("twofa_setup_status") or "") == "queued")
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "active": active,
        "queued": queued,
        "available_workers": max(0, _WORKERS - active),
    }
