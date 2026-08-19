# -*- coding: utf-8 -*-
"""账号页设置密码后台任务。"""
from __future__ import annotations

import logging
import json
import re
import secrets
import string
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import roxybrowser as roxy_cfg
from core import db
from core.roxy_account_task import profile_id_for_account, open_account_profile_with_recovery

logger = logging.getLogger(__name__)

ALLOWED_MODES = {"post_login_add_password", "post_login_password_reset"}
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_OTP_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")


def password_setup_log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"password-setup-{safe}.log"


def _append_password_setup_log(
    email: str,
    line: str,
    *,
    password: str = "",
    level: str = "INFO",
    clear: bool = False,
) -> None:
    try:
        path = password_setup_log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        mode = "w" if clear else "a"
        safe_line = redact_password(str(line or ""), password)
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(f"{stamp} [{level}] {safe_line}\n")
    except Exception:
        logger.exception("password setup log write failed email=%s", email)


def _generate_password(length: int = 14) -> str:
    length = max(8, int(length))
    groups = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    password = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    password.extend(secrets.choice(alphabet) for _ in range(length - len(password)))
    secrets.SystemRandom().shuffle(password)
    return "".join(password)


def default_password_setup_mode() -> str:
    configured = str(getattr(roxy_cfg, "ROXY_PASSWORD_SETUP_MODE", "") or "").strip()
    return configured if configured in ALLOWED_MODES else "post_login_add_password"


def default_password_setup_password() -> str:
    configured = str(getattr(roxy_cfg, "ROXY_PASSWORD_SETUP_PASSWORD", "") or "").strip()
    if configured:
        return configured
    from config import register as register_cfg

    configured = str(getattr(register_cfg, "REGISTER_PASSWORD", "") or "").strip()
    return configured or _generate_password()


def resolve_password_setup_request(mode: str = "", password: str = "") -> tuple[str, str]:
    return validate_password_setup_request(
        mode or default_password_setup_mode(),
        password or default_password_setup_password(),
    )


def _bounded_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(roxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def validate_password_setup_request(mode: str, password: str) -> tuple[str, str]:
    normalized_mode = str(mode or "post_login_add_password").strip()
    if normalized_mode not in ALLOWED_MODES:
        raise ValueError("设置密码模式无效")
    normalized_password = str(password or "")
    if len(normalized_password) < 8:
        raise ValueError("密码长度不能少于 8 位")
    if len(normalized_password) > 256:
        raise ValueError("密码长度不能超过 256 位")
    return normalized_mode, normalized_password


def redact_password(message: str, password: str) -> str:
    text = str(message or "")
    secret = str(password or "")
    if secret:
        text = text.replace(secret, "<redacted>")
    return _OTP_PATTERN.sub("<otp-redacted>", text)[:500]


def _is_retryable_password_setup_error(error: Exception) -> bool:
    """判断设置密码错误是否适合重新排到队尾重试。"""
    if error is None:
        return False
    if error.__class__.__name__ == "PasswordAlreadySetError":
        return False
    if isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        return False
    text = str(error or "").lower()
    permanent_markers = (
        "password format",
        "密码长度",
        "密码格式",
        "模式无效",
        "邮箱为空",
        "账号不存在",
        "password_already_set",
        "already set",
    )
    if any(marker.lower() in text for marker in permanent_markers):
        return False
    transient_markers = (
        "timeout",
        "time out",
        "aborterror",
        "roxy api",
        "http 5",
        "connection",
        "network",
        "proxyerror",
        "otp",
        "验证码",
        "页面",
        "browser/open",
    )
    return isinstance(error, (TimeoutError, ConnectionError, RuntimeError)) or any(
        marker.lower() in text for marker in transient_markers
    )


def _max_password_setup_attempts() -> int:
    return _bounded_int("ROXY_PASSWORD_SETUP_MAX_RETRIES", 3, 1, 10)


def _retry_delay_seconds(attempt: int) -> int:
    """按刚失败的尝试序号返回下一次执行前的退避秒数。"""
    normalized = max(1, int(attempt or 1))
    if normalized == 1:
        return 15
    if normalized == 2:
        return 60
    return 180


def _stored_registration_password(account: dict | None) -> str:
    account = account or {}
    direct = str(account.get("registration_password") or "").strip()
    if direct:
        return direct
    raw = account.get("extra_json")
    if isinstance(raw, dict):
        return str(raw.get("registration_password") or "").strip()
    try:
        extra = json.loads(raw) if str(raw or "").strip() else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    return str(extra.get("registration_password") or "").strip() if isinstance(extra, dict) else ""


def _password_setup_already_done(account: dict | None) -> bool:
    account = account or {}
    raw = account.get("extra_json")
    if isinstance(raw, dict):
        extra = raw
    else:
        try:
            extra = json.loads(raw) if str(raw or "").strip() else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    status = str(account.get("password_setup_status") or extra.get("password_setup_status") or "")
    return bool(_stored_registration_password(account)) or status in {
        "success",
        "already_set",
    }


def _safe_browser_url(driver) -> str:
    try:
        raw = str(getattr(driver, "current_url", "") or "")
    except Exception:
        return "unknown"
    if not raw:
        return "unknown"
    try:
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:300]
    except Exception:
        pass
    return raw.split("?", 1)[0][:300]


def format_password_setup_diagnostic(error, *, opened_profile_id: str = "", driver=None) -> str:
    """按失败阶段生成诊断，避免把浏览器内请求失败误报成 Roxy open 失败。"""
    text = redact_password(str(error or ""), "")
    lowered = text.lower()
    profile = str(opened_profile_id or "") or "unknown"
    if "browser/open" in lowered:
        return (
            "Roxy open diagnostic: "
            f"api_base={getattr(roxy_cfg, 'ROXY_API_BASE', '')} "
            f"open_path={getattr(roxy_cfg, 'ROXY_OPEN_PATH', '')} "
            f"workspace_id={getattr(roxy_cfg, 'ROXY_WORKSPACE_ID', '')} "
            f"profile_id={profile} (profile open API failed)"
        )

    if "stage': 'csrf'" in lowered or 'stage": "csrf"' in lowered or "stage=csrf" in lowered:
        stage = "csrf"
    elif "stage': 'signin'" in lowered or 'stage": "signin"' in lowered or "stage=signin" in lowered:
        stage = "signin"
    else:
        stage = "unknown"
    detail = (
        "CSRF 请求在浏览器内超时并被 AbortController 中止，通常表示当前 Roxy 环境的代理/出口"
        "无法完成 chatgpt.com 请求或页面尚未就绪。"
        if stage == "csrf" and "aborterror" in lowered
        else "请结合当前页面 URL 和错误内容继续排查。"
    )
    return (
        "Browser network diagnostic: "
        f"stage={stage} profile_id={profile} current_url={_safe_browser_url(driver)} "
        f"error={text} {detail}"
    )


def _profile_id(account: dict) -> str:
    return profile_id_for_account(account)


def _is_stale_profile_open_error(exc: Exception) -> bool:
    """判断 Roxy 是否拒绝打开已保存的 profile，允许设置密码任务自动恢复。"""
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "http 404",
        "http 502",
        "http 503",
        "profile not found",
        "dirid",
        "窗口/数据不存在",
        "数据不存在",
    ))


def _open_profile_with_recovery(client, profile_id: str, email: str):
    """打开历史 profile；失效时创建新环境，后续流程会在新环境中重新登录。"""
    return open_account_profile_with_recovery(
        client,
        profile_id,
        progress_callback=lambda message, level="INFO": _append_password_setup_log(
            email, f"[设置密码] {message}", level=level,
        ),
    )


def _run_password_setup_task(*, account_id: int, email: str, mode: str, password: str) -> dict:
    from core.roxy_registration import PasswordAlreadySetError, _build_driver, _run_roxy_password_setup
    from core.roxybrowser_client import RoxyBrowserClient

    driver = None
    opened = None
    client = None
    succeeded = False
    attempt = 1
    max_attempts = _max_password_setup_attempts()
    try:
        _append_password_setup_log(email, f"[设置密码] 开始后台执行 account_id={account_id} mode={mode}")
        if not db.mark_account_password_setup_running(account_id):
            _append_password_setup_log(email, "[设置密码] 任务状态已失效，取消执行", level="WARNING")
            raise RuntimeError("设置密码任务状态已失效")
        account = db.get_account(account_id)
        try:
            attempt = max(1, int((account or {}).get("password_setup_attempt") or 1))
            max_attempts = max(
                1,
                int((account or {}).get("password_setup_max_attempts") or max_attempts),
            )
        except (TypeError, ValueError):
            attempt = 1
        profile_id = _profile_id(account or {})
        _append_password_setup_log(email, "[设置密码] 已读取账号配置")
        if not profile_id:
            _append_password_setup_log(
                email,
                "[设置密码] 账号没有保存 Roxy 环境，自动创建新环境并重新登录",
                level="WARNING",
            )
        else:
            _append_password_setup_log(email, f"[设置密码] 打开 Roxy 环境 profile_id={profile_id}")
        client = RoxyBrowserClient()
        opened = _open_profile_with_recovery(client, profile_id, email)
        _append_password_setup_log(email, "[设置密码] Roxy 环境已打开")
        driver = _build_driver(opened)
        _append_password_setup_log(email, "[设置密码] 开始邮箱重新认证、OTP 验证和提交新密码")
        saved = _run_roxy_password_setup(
            driver,
            email,
            mode=mode,
            password=password,
            progress_callback=lambda message: _append_password_setup_log(
                email,
                message,
                password=password,
            ),
        )
        result = {
            "ok": True,
            "password": saved,
            "retryable": False,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_password_setup(account_id, result)
        succeeded = True
        _append_password_setup_log(email, f"[设置密码] 完成：密码已设置 mode={mode}")
        return result
    except PasswordAlreadySetError:
        result = {
            "ok": True,
            "already_set": True,
            "retryable": False,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_password_setup(account_id, result)
        succeeded = True
        _append_password_setup_log(email, "[设置密码] 检测到密码已经设置，跳过后续设置密码任务")
        return result
    except Exception as exc:
        error_text = redact_password(f"{type(exc).__name__}: {exc}", password)
        result = {
            "ok": False,
            "retryable": _is_retryable_password_setup_error(exc),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "error": error_text,
        }
        try:
            # Keep the failure log actionable without recording the API token or password.
            profile_for_log = getattr(opened, "profile_id", "") or _profile_id(db.get_account(account_id) or {})
            _append_password_setup_log(
                email,
                format_password_setup_diagnostic(
                    error_text,
                    opened_profile_id=profile_for_log,
                    driver=driver,
                ),
                password=password,
                level="ERROR",
            )
        except Exception:
            logger.exception("password setup diagnostic log failed email=%s", email)
        _append_password_setup_log(
            email,
            f"[设置密码] 失败：{error_text}",
            password=password,
            level="ERROR",
        )
        try:
            db.update_account_password_setup(account_id, result)
        except Exception:
            logger.exception("设置密码失败状态写回失败 account_id=%s", account_id)
        return result
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logger.exception("关闭 Roxy 浏览器失败 profile_id=%s", getattr(opened, "profile_id", ""))
        should_cleanup = bool(opened is not None and opened.profile_id and (
            succeeded
            or (
                bool(getattr(opened, "created_by_run", False))
                and bool(getattr(roxy_cfg, "ROXY_PASSWORD_SETUP_DELETE_TEMP_PROFILE_ON_FAILURE", True))
            )
        ))
        if should_cleanup:
            try:
                client.cleanup_profile(opened)
                action = "已关闭并清理 Roxy 临时环境" if not succeeded else "已关闭 Roxy 环境"
                _append_password_setup_log(email, f"[设置密码] {action} profile_id={opened.profile_id}")
            except Exception:
                logger.exception("关闭 Roxy 环境失败 profile_id=%s", opened.profile_id)


_WORKERS = _bounded_int("ROXY_PASSWORD_SETUP_WORKERS", 1, 1, 16)
_QUEUE_LIMIT = _bounded_int("ROXY_PASSWORD_SETUP_QUEUE_LIMIT", 100, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="password-setup")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_LOCK = threading.Lock()
_ACTIVE: set[int] = set()


def enqueue_account_password_setup(
    *, account_id: int, mode: str, password: str, trigger: str = "manual"
) -> dict:
    mode, password = resolve_password_setup_request(mode, password)
    account = db.get_account(int(account_id))
    if not account:
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    email = str(account.get("email") or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "账号邮箱为空"}
    if _password_setup_already_done(account):
        status = str(account.get("password_setup_status") or "success")
        return {
            "accepted": False,
            "skipped": True,
            "already_set": True,
            "busy": False,
            "account_id": int(account_id),
            "email": email,
            "status": status if status in {"success", "already_set"} else "success",
            "started_count": 0,
            "skipped_count": 1,
            "error": "账号密码已经设置，已跳过设置密码任务",
        }
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "设置密码队列已满，请稍后重试"}
    max_attempts = _max_password_setup_attempts()
    if not db.claim_account_password_setup(
        int(account_id),
        mode=mode,
        trigger=trigger,
        max_attempts=max_attempts,
    ):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在设置密码"}
    _append_password_setup_log(
        email,
        f"[设置密码] 已入队 account_id={int(account_id)} mode={mode} trigger={trigger}",
        clear=True,
    )
    try:
        future = _EXECUTOR.submit(
            _run_task_wrapper,
            account_id=int(account_id),
            email=email,
            mode=mode,
            password=password,
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        enqueue_error = redact_password(f"入队失败: {type(exc).__name__}: {exc}", password)
        _append_password_setup_log(email, f"[设置密码] {enqueue_error}", level="ERROR")
        db.update_account_password_setup(int(account_id), {"ok": False, "error": enqueue_error})
        return {"accepted": False, "busy": False, "error": "设置密码入队失败"}
    return {
        "accepted": True,
        "busy": False,
        "account_id": int(account_id),
        "email": email,
        "status": "queued",
        "trigger": trigger,
        "started_count": 1,
        "skipped_count": 0,
        "future": future,
    }


def _schedule_password_setup_retry(
    *, account_id: int, email: str, mode: str, password: str, result: dict
) -> bool:
    attempt = max(1, int(result.get("attempt") or 1))
    max_attempts = max(1, int(result.get("max_attempts") or _max_password_setup_attempts()))
    if not result.get("retryable") or attempt >= max_attempts:
        return False

    next_attempt = attempt + 1
    error = redact_password(str(result.get("error") or "设置密码失败")[:500], password)
    delay = _retry_delay_seconds(attempt)
    retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
    if not db.requeue_account_password_setup(
        account_id,
        error,
        attempt=next_attempt,
        max_attempts=max_attempts,
        next_retry_at=retry_at,
    ):
        return False

    def mark_retry_failed(*, reason: str, error_type: str) -> bool:
        safe_error = redact_password(f"{reason}: {error_type}", password)
        updated = False
        try:
            updated = bool(db.update_account_password_setup(
                account_id,
                {"ok": False, "error": safe_error},
            ))
        except Exception:
            logger.exception("设置密码重试失败状态写回失败 account_id=%s", account_id)
        _append_password_setup_log(
            email,
            f"[设置密码] 自动重试终止 attempt={next_attempt}/{max_attempts} "
            f"delay=0 next_retry_at=None error_type={error_type}",
            level="ERROR",
        )
        return updated

    def arm_timer(timer_delay: int) -> bool:
        try:
            timer = threading.Timer(timer_delay, submit_retry)
            timer.daemon = True
            timer.start()
        except Exception as exc:
            mark_retry_failed(reason="自动重试定时器启动失败", error_type=type(exc).__name__)
            return False
        return True

    def submit_retry() -> None:
        if not _QUEUE_SLOTS.acquire(blocking=False):
            deferred_at = (datetime.now() + timedelta(seconds=5)).isoformat(timespec="seconds")
            try:
                requeued = db.requeue_account_password_setup(
                    account_id,
                    error,
                    attempt=next_attempt,
                    max_attempts=max_attempts,
                    next_retry_at=deferred_at,
                )
            except Exception as exc:
                if not mark_retry_failed(
                    reason="自动重试状态更新异常",
                    error_type=type(exc).__name__,
                ):
                    arm_timer(5)
                return
            if requeued:
                _append_password_setup_log(
                    email,
                    f"[设置密码] 重试等待队列槽 attempt={next_attempt}/{max_attempts} "
                    f"delay=5 next_retry_at={deferred_at}",
                    level="WARNING",
                )
                arm_timer(5)
            else:
                mark_retry_failed(reason="自动重试状态更新失败", error_type="RequeueRejected")
            return

        slot_owned = True
        try:
            try:
                requeued = db.requeue_account_password_setup(
                    account_id,
                    error,
                    attempt=next_attempt,
                    max_attempts=max_attempts,
                    next_retry_at=None,
                )
            except Exception as exc:
                if not mark_retry_failed(
                    reason="自动重试状态更新异常",
                    error_type=type(exc).__name__,
                ):
                    arm_timer(5)
                return
            if not requeued:
                mark_retry_failed(
                    reason="自动重试状态更新失败",
                    error_type="RequeueRejected",
                )
                return
            try:
                _EXECUTOR.submit(
                    _run_task_wrapper,
                    account_id=account_id,
                    email=email,
                    mode=mode,
                    password=password,
                )
            except Exception as exc:
                if not mark_retry_failed(
                    reason="重试入队失败",
                    error_type=type(exc).__name__,
                ):
                    arm_timer(5)
                return
            slot_owned = False
        finally:
            if slot_owned:
                _QUEUE_SLOTS.release()
        _append_password_setup_log(
            email,
            f"[设置密码] 已提交自动重试 attempt={next_attempt}/{max_attempts} "
            "delay=0 next_retry_at=None",
            level="WARNING",
        )

    _append_password_setup_log(
        email,
        f"[设置密码] 已安排自动重试 attempt={next_attempt}/{max_attempts} "
        f"delay={delay} next_retry_at={retry_at} error={error}",
        level="WARNING",
    )
    return arm_timer(delay)


def _run_task_wrapper(*, account_id: int, email: str, mode: str, password: str) -> dict:
    with _LOCK:
        _ACTIVE.add(int(account_id))
    result = {}
    try:
        result = _run_password_setup_task(account_id=account_id, email=email, mode=mode, password=password)
        return result
    finally:
        with _LOCK:
            _ACTIVE.discard(int(account_id))
        _QUEUE_SLOTS.release()
        if result and not result.get("ok"):
            try:
                _schedule_password_setup_retry(
                    account_id=account_id,
                    email=email,
                    mode=mode,
                    password=password,
                    result=result,
                )
            except Exception:
                logger.exception("设置密码自动重试调度失败 account_id=%s", account_id)


def queue_settings() -> dict:
    with _LOCK:
        active = len(_ACTIVE)
    rows = db.list_accounts(limit=5000, archived="all")
    queued_rows = [row for row in rows if str(row.get("password_setup_status") or "") == "queued"]
    now = datetime.now()

    def is_delayed(row: dict) -> bool:
        raw = str(row.get("password_setup_next_retry_at") or "").strip()
        if not raw:
            return False
        try:
            retry_at = datetime.fromisoformat(raw)
            current = datetime.now(retry_at.tzinfo) if retry_at.tzinfo else now
            return retry_at > current
        except (TypeError, ValueError):
            return False

    delayed_rows = [row for row in queued_rows if is_delayed(row)]
    waiting_rows = [row for row in queued_rows if not is_delayed(row)]
    waiting_rows.sort(key=lambda row: (
        str(row.get("password_setup_queued_at") or ""),
        int(row.get("id") or 0),
    ))
    positions = {
        str(row.get("id")): index
        for index, row in enumerate(waiting_rows, start=1)
        if row.get("id") is not None
    }
    queued = len(queued_rows)
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "active": active,
        "queued": queued,
        "waiting": len(waiting_rows),
        "delayed": len(delayed_rows),
        "available_workers": max(0, _WORKERS - active),
        "positions": positions,
    }
