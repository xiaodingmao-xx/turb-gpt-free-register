# -*- coding: utf-8 -*-
"""GCash 资格查询后台队列与脱敏日志。"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from config import gcash as gcash_cfg
from core import db
from core.chatgpt_plan import resolve_plan_check_route
from core.gcash_eligibility import (
    check_account_gcash,
    format_gcash_phase,
    safe_gcash_log_text,
)

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"gcash-check-{safe}.log"


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(f"{stamp} [INFO] {safe_gcash_log_text(line, 1000)}\n")


def _bounded_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(gcash_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _bounded_float(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(gcash_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _bounded_int("GCASH_CHECK_WORKERS", 1, 1, 8)
_QUEUE_LIMIT = _bounded_int("GCASH_CHECK_QUEUE_LIMIT", 100, _WORKERS, 1000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="gcash-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = scheduled + 0.4
    if scheduled > now:
        time.sleep(scheduled - now)


def _run_gcash_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
) -> dict[str, Any]:
    try:
        if not db.mark_account_gcash_check_running(account_id):
            return {"ok": False, "conclusive": False, "decision": "unknown", "error": "账号已删除或 GCash 查询状态已被重置"}

        _append_log(
            email,
            format_gcash_phase("worker_start", account_id=account_id, trigger=trigger, status="started"),
        )
        route = resolve_plan_check_route()
        _append_log(
            email,
            format_gcash_phase(
                "route",
                network_route=route.get("network_route"),
                proxy_mode=route.get("proxy_mode"),
                proxy_used=route.get("proxy_used"),
                proxy_ip=(route.get("proxy") or "").split("@")[-1].split(":")[0] if route.get("proxy") else None,
                fallback_reason=route.get("proxy_fallback_reason"),
            ),
        )
        _wait_for_rate_slot()

        def progress(message: object) -> None:
            _append_log(email, safe_gcash_log_text(message, 1000))

        result = dict(
            check_account_gcash(
                access_token,
                proxy=route.get("proxy", ""),
                timeout=_bounded_float("GCASH_CHECK_TIMEOUT", 20.0, 1.0, 120.0),
                max_attempts=_bounded_int("GCASH_CHECK_MAX_ATTEMPTS", 2, 1, 5),
                retry_delay=_bounded_float("GCASH_CHECK_RETRY_DELAY", 2.0, 0.0, 30.0),
                trial_days=_bounded_int("GCASH_CHECK_TRIAL_DAYS", 0, 0, 365),
                progress_callback=progress,
            )
            or {}
        )
        # 保留路由解析阶段的真实策略，尤其是 auto 模式下的 direct_fallback。
        result["network_route"] = route.get("network_route")
        result["proxy_used"] = route.get("proxy_used")
        result["proxy_fallback_reason"] = route.get("proxy_fallback_reason")

        _append_log(
            email,
            format_gcash_phase(
                "result",
                status="success" if result.get("ok") else "failed",
                ok=bool(result.get("ok")),
                decision=result.get("decision"),
                gcash_available=result.get("gcash_available"),
                trial_eligible=result.get("trial_eligible"),
                actual_trial=result.get("actual_trial"),
                payment_methods=result.get("payment_methods"),
                payment_method_status=result.get("payment_method_status"),
                currency=result.get("currency"),
                amount_due=result.get("amount_due"),
                stripe_mode=result.get("stripe_mode"),
                http_status=result.get("http_status"),
                network_route=result.get("network_route"),
                proxy_used=result.get("proxy_used"),
                proxy_ip=result.get("proxy_ip"),
                attempt=f"{result.get('attempt_count') or '-'}/{result.get('max_attempts') or '-'}",
                retryable=result.get("retryable"),
                error=result.get("error") or "-",
            ),
        )
        _append_log(
            email,
            format_gcash_phase("persist", status="started", fields="gcash_check_status,gcash_available,gcash_payment_methods"),
        )
        persisted = bool(db.update_account_gcash_check(acc_id=account_id, result=result))
        _append_log(
            email,
            format_gcash_phase(
                "persist",
                status="success" if persisted else "failed",
                fields="gcash_check_status,gcash_available,gcash_payment_methods",
            ),
        )
        return result
    except Exception as exc:
        safe_error = f"{type(exc).__name__}: {safe_gcash_log_text(exc, 300)}"
        result = {
            "ok": False,
            "conclusive": False,
            "decision": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": safe_error,
        }
        _append_log(email, format_gcash_phase("terminal", status="failed", error=safe_error))
        try:
            db.update_account_gcash_check(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[GCash] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[GCash] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_gcash_check(
    account_id: int,
    email: str,
    access_token: str,
    *,
    trigger: str = "manual",
) -> dict[str, Any]:
    """提交一个 GCash 资格查询；重复账号或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not email:
        return {"accepted": False, "busy": False, "error": "账号缺少 email"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "GCash 查询队列已满，请稍后重试"}
    if not db.claim_account_gcash_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询 GCash 资格"}

    _append_log(
        email,
        format_gcash_phase("queue", account_id=account_id, trigger=trigger, status="queued"),
        clear=True,
    )
    try:
        _EXECUTOR.submit(
            _run_gcash_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "conclusive": False,
            "decision": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"GCash 查询入队失败: {type(exc).__name__}: {safe_gcash_log_text(exc, 160)}",
        }
        db.update_account_gcash_check(acc_id=account_id, result=result)
        _append_log(email, format_gcash_phase("queue", account_id=account_id, status="failed", error=result["error"]))
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }

def queue_status() -> dict[str, Any]:
    snapshot = db.list_account_gcash_check_statuses(limit=max(5000, _QUEUE_LIMIT * 10))
    items = list(snapshot.get("items") or []) if isinstance(snapshot, dict) else []
    running = [item for item in items if item.get("gcash_check_status") == "running"]
    queued = [item for item in items if item.get("gcash_check_status") == "queued"]
    def compact(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "account_id": item.get("id"),
            "id": item.get("id"),
            "email": item.get("email"),
            "status": item.get("gcash_check_status"),
            "queued_at": item.get("gcash_check_queued_at"),
            "started_at": item.get("gcash_check_started_at"),
        }
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "active_count": len(running),
        "running_count": len(running),
        "queued_count": len(queued),
        "running": [compact(item) for item in running],
        "queued": [compact(item) for item in queued],
        "active": [compact(item) for item in running],
    }
