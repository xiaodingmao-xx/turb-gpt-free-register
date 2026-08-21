# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from config import roxybrowser as roxy_cfg
from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import resolve_plan_check_route
from core.roxy_live_check import check_account_liveness_with_roxy, safe_error_text

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(roxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_live_check_mode(value: str | None) -> str:
    mode = str(value or "protocol").strip().lower()
    if mode not in {"protocol", "browser"}:
        raise ValueError("查活方式只支持 protocol 或 browser")
    return mode


_BROWSER_WORKERS = _bounded_int("LIVE_CHECK_BROWSER_WORKERS", 1, 1, 4)
_BROWSER_QUEUE_LIMIT = _bounded_int(
    "LIVE_CHECK_BROWSER_QUEUE_LIMIT", 100, _BROWSER_WORKERS, 500
)
_BROWSER_EXECUTOR = ThreadPoolExecutor(
    max_workers=_BROWSER_WORKERS, thread_name_prefix="browser-live-check"
)
_BROWSER_QUEUE_SLOTS = threading.BoundedSemaphore(_BROWSER_QUEUE_LIMIT)


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        route = resolve_plan_check_route(explicit_proxy=proxy)
        selected_proxy = route.get("proxy")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        result = check_account_liveness(email, proxy=selected_proxy, clear_log=False)
        # 早期 providers/csrf 403 通常是该出口被 CF 拦截，不代表账号死亡。
        # auto/proxy 模式下如果用了代理，额外直连兜底一次，便于和套餐查询的 auto 语义保持接近。
        err_text = str(result.get("error") or "")
        if (
            not result.get("ok")
            and result.get("status") == "failed"
            and "403" in err_text
            and selected_proxy
            and str(route.get("network_route") or "") == "proxy"
        ):
            _append_log(email, "[查活] 代理出口收到 403，尝试直连兜底一次")
            result = check_account_liveness(email, proxy="", clear_log=False)
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
        elif result.get("status") == "deactivated":
            _append_log(email, f"[查活] 完成：账号已废 {result.get('error') or ''}")
        else:
            _append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def _browser_retry_delays() -> list[int]:
    values = []
    for raw in str(getattr(roxy_cfg, "LIVE_CHECK_BROWSER_RETRY_DELAYS", "15,60,180") or "").split(","):
        try:
            values.append(max(0, min(3600, int(raw.strip()))))
        except (TypeError, ValueError):
            continue
    return values or [15, 60, 180]


def _browser_retry_delay(attempt: int) -> int:
    values = _browser_retry_delays()
    return values[min(max(1, int(attempt)) - 1, len(values) - 1)]


def _browser_max_attempts() -> int:
    return _bounded_int("LIVE_CHECK_BROWSER_MAX_ATTEMPTS", 3, 1, 10)


def _browser_failure_result(error: object, *, failure_kind: str = "unknown", attempt: int = 1, max_attempts: int | None = None) -> dict:
    return {
        "ok": False,
        "status": "failed",
        "backend": "browser",
        "failure_kind": failure_kind,
        "retryable": True,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error": f"{type(error).__name__}: {safe_error_text(error)}",
        "attempt": max(1, int(attempt or 1)),
        "max_attempts": max(1, int(max_attempts or _browser_max_attempts())),
    }


def _run_browser_live_check(*, account_id: int, email: str, trigger: str) -> dict:
    """执行一次 Roxy 浏览器查活，不在此处决定是否延迟重试。"""
    if not db.mark_account_live_check_running(account_id):
        return _browser_failure_result(
            "账号已删除或查活状态已被重置",
            failure_kind="unknown",
            attempt=1,
            max_attempts=_browser_max_attempts(),
        )
    account = db.get_account(account_id) or {}
    attempt = max(1, int(account.get("live_check_attempt") or 1))
    max_attempts = max(1, int(account.get("live_check_max_attempts") or _browser_max_attempts()))
    _append_log(
        email,
        f"[浏览器查活] phase=driver_start account_id={account_id} status=started "
        f"trigger={trigger} attempt={attempt}/{max_attempts}",
    )
    try:
        result = check_account_liveness_with_roxy(
            account_id,
            email,
            progress_callback=lambda message: _append_log(email, str(message)),
        )
    except Exception as exc:
        result = _browser_failure_result(
            exc,
            failure_kind="unknown",
            attempt=attempt,
            max_attempts=max_attempts,
        )
    result = dict(result or {})
    result.setdefault("backend", "browser")
    result.setdefault("failure_kind", None if result.get("ok") else "unknown")
    result.setdefault("retryable", False if result.get("ok") else True)
    result.setdefault("checked_at", datetime.now().isoformat(timespec="seconds"))
    result["attempt"] = attempt
    result["max_attempts"] = max_attempts
    return result


def _mark_browser_terminal(account_id: int, email: str, result: dict) -> None:
    persisted = False
    _append_log(
        email,
        f"[浏览器查活] phase=token_persist account_id={account_id} status=started "
        f"fields=access_token,session result_ok={bool(result.get('ok'))}",
    )
    try:
        persisted = bool(db.update_account_liveness(account_id, result))
    except Exception as exc:
        logger.exception("[浏览器查活] 写入终态失败 account_id=%s: %s", account_id, exc)
    _append_log(
        email,
        f"[浏览器查活] phase=token_persist account_id={account_id} "
        f"status={'success' if persisted else 'failed'} fields=access_token,session",
    )
    try:
        if result.get("ok"):
            _append_log(
                email,
                f"[浏览器查活] phase=terminal account_id={account_id} status=live "
                "failure_kind=- retryable=false message=账号正常，已刷新 Token",
            )
        elif result.get("status") == "deactivated":
            _append_log(
                email,
                f"[浏览器查活] phase=terminal account_id={account_id} status=deactivated "
                f"failure_kind={result.get('failure_kind') or 'unknown'} retryable=false "
                f"message={safe_error_text(result.get('error') or '')}",
            )
        else:
            _append_log(
                email,
                f"[浏览器查活] phase=terminal account_id={account_id} status=failed "
                f"failure_kind={result.get('failure_kind') or 'unknown'} "
                f"retryable={bool(result.get('retryable'))} "
                f"message={safe_error_text(result.get('error') or '')}",
            )
    except Exception:
        pass


def _schedule_browser_retry(*, account_id: int, email: str, trigger: str, result: dict) -> bool:
    attempt = max(1, int(result.get("attempt") or 1))
    max_attempts = max(1, int(result.get("max_attempts") or _browser_max_attempts()))
    if not bool(result.get("retryable")) or attempt >= max_attempts:
        return False
    delay = _browser_retry_delay(attempt)
    next_retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    if not db.requeue_account_live_check(
        account_id,
        str(result.get("error") or "查活失败"),
        failure_kind=str(result.get("failure_kind") or "unknown"),
        attempt=attempt + 1,
        max_attempts=max_attempts,
        next_retry_at=next_retry_at,
    ):
        return False
    _append_log(
        email,
        f"[浏览器查活] phase=retry account_id={account_id} status=scheduled "
        f"attempt={attempt + 1}/{max_attempts} delay={delay}s next_retry_at={next_retry_at} "
        f"failure_kind={result.get('failure_kind') or 'unknown'} retryable=true",
    )

    def arm_timer(seconds: float) -> bool:
        try:
            timer = threading.Timer(seconds, on_timer)
            timer.daemon = True
            timer.start()
            return True
        except Exception as exc:
            terminal = dict(result)
            terminal.update({
                "retryable": False,
                "failure_kind": "unknown",
                "error": f"查活延迟重试定时器失败: {type(exc).__name__}: {str(exc)[:300]}",
            })
            _mark_browser_terminal(account_id, email, terminal)
            return False

    def on_timer() -> None:
        if not _BROWSER_QUEUE_SLOTS.acquire(blocking=False):
            arm_timer(5)
            return
        try:
            _BROWSER_EXECUTOR.submit(
                _run_browser_task_wrapper,
                account_id=account_id,
                email=email,
                trigger=trigger,
            )
        except Exception as exc:
            _BROWSER_QUEUE_SLOTS.release()
            terminal = dict(result)
            terminal.update({
                "retryable": False,
                "failure_kind": "unknown",
                "error": f"查活重试入队失败: {type(exc).__name__}: {str(exc)[:300]}",
            })
            _mark_browser_terminal(account_id, email, terminal)

    return arm_timer(delay)


def _run_browser_task_wrapper(*, account_id: int, email: str, trigger: str) -> dict:
    result = None
    schedule = False
    try:
        result = _run_browser_live_check(account_id=account_id, email=email, trigger=trigger)
        schedule = (
            not bool(result.get("ok"))
            and bool(result.get("retryable"))
            and int(result.get("attempt") or 1) < int(result.get("max_attempts") or _browser_max_attempts())
        )
    except Exception as exc:
        result = _browser_failure_result(exc, failure_kind="unknown")
    finally:
        _BROWSER_QUEUE_SLOTS.release()

    if schedule and _schedule_browser_retry(
        account_id=account_id,
        email=email,
        trigger=trigger,
        result=result,
    ):
        return result
    _mark_browser_terminal(account_id, email, result)
    return result


def enqueue_account_live_check(
    *,
    account_id: int,
    email: str,
    trigger: str = "manual",
    proxy: str | None = None,
    mode: str = "protocol",
) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    try:
        normalized_mode = normalize_live_check_mode(mode)
    except ValueError as exc:
        return {"accepted": False, "busy": False, "error": str(exc)}
    slots = _BROWSER_QUEUE_SLOTS if normalized_mode == "browser" else _QUEUE_SLOTS
    executor = _BROWSER_EXECUTOR if normalized_mode == "browser" else _EXECUTOR
    max_attempts = _browser_max_attempts() if normalized_mode == "browser" else 1
    if not slots.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(
        acc_id=account_id,
        trigger=trigger,
        backend=normalized_mode,
        max_attempts=max_attempts,
    ):
        slots.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    log_prefix = "[浏览器查活]" if normalized_mode == "browser" else "[查活]"
    _append_log(
        email,
        f"{log_prefix} phase=queue account_id={account_id} status=queued "
        f"trigger={trigger} mode={normalized_mode}",
        clear=True,
    )
    try:
        if normalized_mode == "browser":
            executor.submit(
                _run_browser_task_wrapper,
                account_id=account_id,
                email=email,
                trigger=str(trigger or "manual"),
            )
        else:
            executor.submit(
                _run_live_check,
                account_id=account_id,
                email=email,
                proxy=proxy,
                trigger=str(trigger or "manual"),
            )
    except Exception as exc:
        slots.release()
        result = {
            "ok": False,
            "status": "failed",
            "backend": normalized_mode,
            "failure_kind": "unknown",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
        "mode": normalized_mode,
    }


def queue_settings(mode: str = "protocol") -> dict:
    normalized = normalize_live_check_mode(mode)
    if normalized == "browser":
        return {
            "backend": "browser",
            "workers": _BROWSER_WORKERS,
            "queue_limit": _BROWSER_QUEUE_LIMIT,
            "max_attempts": _browser_max_attempts(),
            "retry_delays": _browser_retry_delays(),
        }
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _is_future_retry(value: object, now: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed > now
    except (TypeError, ValueError):
        return False


def queue_status(mode: str = "browser") -> dict:
    """返回指定查活后端的容量、当前执行和排队快照。"""
    normalized = normalize_live_check_mode(mode)
    settings = queue_settings(normalized)
    rows = db.list_accounts(
        limit=5000,
        offset=0,
        archived="all",
        sort_key="id",
        sort_order="asc",
    )
    active_rows = [
        row for row in rows
        if str(row.get("live_check_backend") or "") == normalized
        and str(row.get("live_check_status") or "") == "running"
    ]
    queued_rows = [
        row for row in rows
        if str(row.get("live_check_backend") or "") == normalized
        and str(row.get("live_check_status") or "") == "queued"
    ]
    now = datetime.now()
    delayed_rows = [
        row for row in queued_rows
        if _is_future_retry(row.get("live_check_next_retry_at"), now)
    ]
    delayed_ids = {id(row) for row in delayed_rows}
    waiting_rows = [row for row in queued_rows if id(row) not in delayed_ids]
    waiting_rows.sort(
        key=lambda row: (
            str(row.get("live_check_queued_at") or ""),
            int(row.get("id") or 0),
        )
    )
    running_accounts = [
        {
            "id": row.get("id"),
            "email": row.get("email"),
            "started_at": row.get("live_check_started_at"),
            "attempt": row.get("live_check_attempt") or 1,
            "max_attempts": row.get("live_check_max_attempts") or settings.get("max_attempts", 1),
        }
        for row in sorted(active_rows, key=lambda item: int(item.get("id") or 0))
    ]
    return {
        "backend": normalized,
        **settings,
        "active": len(running_accounts),
        "queued": len(queued_rows),
        "waiting": len(waiting_rows),
        "delayed": len(delayed_rows),
        "available_workers": max(0, int(settings["workers"]) - len(running_accounts)),
        "running_accounts": running_accounts,
        "positions": {
            str(row.get("id")): index
            for index, row in enumerate(waiting_rows, 1)
        },
    }
