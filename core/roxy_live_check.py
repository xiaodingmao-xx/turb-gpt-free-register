# -*- coding: utf-8 -*-
"""Roxy 真实浏览器查活的认证、身份校验和生命周期工具。"""
from __future__ import annotations

import re
import time
import json
import logging
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from config import roxybrowser as roxy_cfg
from core import db
from core.chatgpt_plan import token_claims
from core.openai_auth import detect_account_unusable_text
from core.roxy_registration import (
    RoxyExistingLoginError,
    _build_driver,
    login_existing_account_with_otp,
)
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)


class RoxyLiveCheckFailure(RuntimeError):
    def __init__(
        self,
        failure_kind: str,
        message: str,
        *,
        retryable: bool,
        deactivated: bool = False,
    ):
        super().__init__(message)
        self.failure_kind = str(failure_kind or "unknown")
        self.retryable = bool(retryable)
        self.deactivated = bool(deactivated)


def safe_url_for_log(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"


def safe_error_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s]+", lambda match: safe_url_for_log(match.group(0)), text)
    text = re.sub(
        r'(?i)(["\']?(?:accessToken|authorization|cookie|proxyUserName|proxyPassword|password|passwd|token|secret|otp|code|state)["\']?\s*:\s*)(["\'][^"\']*["\']|[^,\s}]+)',
        r'\1"<redacted>"',
        text,
    )
    text = re.sub(
        r"(?i)((?:accessToken|authorization|cookie|proxyUserName|proxyPassword|password|passwd|token|secret|otp|code|state)\s*[:=]\s*)[^\s,}]+",
        r"\1<redacted>",
        text,
    )
    return text[:500]


def validate_browser_session(
    session_info: dict,
    account: dict,
    email: str,
    *,
    now_ts: float | None = None,
) -> dict:
    session_info = session_info if isinstance(session_info, dict) else {}
    account = account if isinstance(account, dict) else {}
    token = str(session_info.get("accessToken") or "").strip()
    if not token:
        raise RoxyLiveCheckFailure("session_missing", "登录后未取得 accessToken", retryable=True)

    target_email = str(email or "").strip().lower()
    user = session_info.get("user") if isinstance(session_info.get("user"), dict) else {}
    session_email = str(user.get("email") or "").strip().lower()
    if not session_email or session_email != target_email:
        raise RoxyLiveCheckFailure(
            "profile_account_mismatch", "Roxy profile 登录邮箱与目标账号不一致", retryable=False
        )

    claims = token_claims(token)
    claim_email = str(claims.get("email") or "").strip().lower()
    if claim_email and claim_email != target_email:
        raise RoxyLiveCheckFailure(
            "profile_account_mismatch", "Token 邮箱与目标账号不一致", retryable=False
        )

    session_user_id = str(user.get("id") or "").strip()
    claim_user_id = str(claims.get("user_id") or "").strip()
    stored_user_id = str(account.get("user_id") or "").strip()
    if claim_user_id and session_user_id and claim_user_id != session_user_id:
        raise RoxyLiveCheckFailure(
            "account_identity_mismatch", "Token user_id 与 session 不一致", retryable=False
        )
    if stored_user_id and session_user_id and stored_user_id != session_user_id:
        raise RoxyLiveCheckFailure(
            "account_identity_mismatch", "Session user_id 与账号记录不一致", retryable=False
        )

    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and float(exp) <= float(now_ts if now_ts is not None else time.time()):
        raise RoxyLiveCheckFailure("session_expired", "新取得的 accessToken 已过期", retryable=True)
    return session_info


def _profile_id(account: dict) -> str:
    raw = account.get("extra_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    roxy = raw.get("roxybrowser") if isinstance(raw, dict) else {}
    return str((roxy or {}).get("profile_id") or "").strip()


def _is_stale_profile_error(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "http 404", "http 502", "http 503", "profile not found", "dirid", "数据不存在", "窗口/数据不存在"
    ))


def _masked_proxy(opened) -> str | None:
    raw = getattr(opened, "raw", {}) if opened is not None else {}
    info = raw.get("proxyInfo") if isinstance(raw, dict) else {}
    info = info if isinstance(info, dict) else {}
    host = str(info.get("host") or "").strip()
    port = str(info.get("port") or "").strip()
    protocol = str(info.get("protocol") or info.get("proxyCategory") or "").strip().lower()
    if not host:
        return None
    scheme = "socks5" if protocol == "socks5" else protocol or "http"
    return f"{scheme}://{host}{':' + port if port else ''}"


def _open_for_live_check(client, saved_profile_id: str, progress):
    if not saved_profile_id:
        temporary_id = client.create_profile()
        return client.open_profile(temporary_id, allow_existing_profile=True), "temporary"
    try:
        return client.open_profile(saved_profile_id, allow_existing_profile=True), "saved"
    except Exception as exc:
        if not _is_stale_profile_error(exc):
            raise RoxyLiveCheckFailure(
                "browser_open_failed", safe_error_text(exc), retryable=True
            ) from exc
        progress("[浏览器查活] 历史 profile 已失效，创建一个临时环境")
        temporary_id = client.create_profile()
        return client.open_profile(temporary_id, allow_existing_profile=True), "temporary"


def _failed_result(
    kind: str,
    error: object,
    *,
    retryable: bool,
    deactivated: bool = False,
    profile_id: str | None = None,
    profile_source: str | None = None,
    proxy_used: str | None = None,
) -> dict:
    result = {
        "ok": False,
        "status": "deactivated" if deactivated else "failed",
        "backend": "browser",
        "failure_kind": str(kind or "unknown"),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "retryable": bool(retryable),
        "error": safe_error_text(error),
    }
    if profile_id:
        result["profile_id"] = profile_id
    if profile_source:
        result["profile_source"] = profile_source
    if proxy_used:
        result["proxy_used"] = proxy_used
    return result


def _browser_exception_kind(exc: BaseException) -> str:
    text = str(exc or "").lower()
    if any(marker in text for marker in (
        "timeout", "timed out", "connection", "network", "http 403", "http 429",
        "http 500", "http 502", "http 503", "http 504", "disconnected", "reset",
    )):
        return "network_unavailable"
    if any(marker in text for marker in ("selenium", "webdriver", "driver", "browser")):
        return "browser_open_failed"
    return "unknown"


def check_account_liveness_with_roxy(
    account_id: int,
    email: str,
    *,
    progress_callback=None,
) -> dict:
    """在 Roxy 真实浏览器中验证目标账号并返回统一查活结果。"""
    progress = progress_callback or (lambda message: None)
    target_email = str(email or "").strip()
    checked_at = datetime.now().isoformat(timespec="seconds")
    account = db.get_account(int(account_id))
    if not account:
        return _failed_result("unknown", "账号不存在", retryable=False)
    stored_email = str(account.get("email") or "").strip()
    if not stored_email or stored_email.lower() != target_email.lower():
        return _failed_result("account_identity_mismatch", "查活邮箱与账号记录不一致", retryable=False)

    client = RoxyBrowserClient()
    opened = None
    driver = None
    profile_source = None
    profile_id = None
    proxy_used = None
    result = None
    try:
        saved_profile_id = _profile_id(account)
        opened, profile_source = _open_for_live_check(client, saved_profile_id, progress)
        profile_id = str(getattr(opened, "profile_id", "") or "").strip() or None
        proxy_used = _masked_proxy(opened)
        progress(
            f"[浏览器查活] 已打开 profile={profile_id or '-'} source={profile_source} "
            f"proxy={proxy_used or '-'}"
        )
        driver = _build_driver(opened)
        try:
            driver.set_page_load_timeout(int(getattr(roxy_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90))
        except Exception:
            pass
        session_info = login_existing_account_with_otp(
            driver,
            target_email,
            progress_callback=progress,
        )
        validated = validate_browser_session(session_info, account, target_email)
        result = {
            "ok": True,
            "status": "live",
            "backend": "browser",
            "failure_kind": None,
            "checked_at": checked_at,
            "retryable": False,
            "access_token": validated.get("accessToken"),
            "session": validated,
            "profile_id": profile_id,
            "profile_source": profile_source,
            "proxy_used": proxy_used,
        }
        progress("[浏览器查活] session 身份校验通过，已取得最新登录态")
        return result
    except RoxyExistingLoginError as exc:
        result = _failed_result(
            exc.failure_kind,
            exc,
            retryable=exc.retryable,
            profile_id=profile_id,
            profile_source=profile_source,
            proxy_used=proxy_used,
        )
        return result
    except RoxyLiveCheckFailure as exc:
        result = _failed_result(
            exc.failure_kind,
            exc,
            retryable=exc.retryable,
            deactivated=exc.deactivated,
            profile_id=profile_id,
            profile_source=profile_source,
            proxy_used=proxy_used,
        )
        return result
    except Exception as exc:
        dead_code = detect_account_unusable_text(str(exc))
        if dead_code:
            result = _failed_result(
                "account_unusable",
                dead_code,
                retryable=False,
                deactivated=True,
                profile_id=profile_id,
                profile_source=profile_source,
                proxy_used=proxy_used,
            )
        else:
            kind = _browser_exception_kind(exc)
            result = _failed_result(
                kind,
                exc,
                retryable=True,
                profile_id=profile_id,
                profile_source=profile_source,
                proxy_used=proxy_used,
            )
        logger.warning("[浏览器查活] 失败 email=%s kind=%s error=%s", target_email, result["failure_kind"], result["error"])
        return result
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                logger.warning("[浏览器查活] 关闭 driver 失败：%s", safe_error_text(exc))
        if opened is not None and profile_id:
            try:
                client.close_profile(profile_id)
            except Exception as exc:
                logger.warning("[浏览器查活] 关闭 profile 失败：%s", safe_error_text(exc))
            if (
                profile_source == "temporary"
                and bool(getattr(roxy_cfg, "LIVE_CHECK_BROWSER_DELETE_TEMP_PROFILE", True))
                and not bool(getattr(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
            ):
                try:
                    client.delete_profile(profile_id)
                except Exception as exc:
                    logger.warning("[浏览器查活] 删除临时 profile 失败：%s", safe_error_text(exc))
