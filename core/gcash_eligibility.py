# -*- coding: utf-8 -*-
"""安全探测 ChatGPT 账号的 GCash 支付方式资格。

本模块只创建未确认的 Checkout Session 并读取 Stripe 初始化信息，绝不确认
Checkout、创建 PaymentMethod 或提交付款。原始响应和敏感凭据只在当前调用的
内存中短暂使用，返回值仅包含脱敏后的派生字段。
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

from core.chatgpt_plan import (
    _mask_proxy,
    _proxy_log_fields,
    normalize_token,
    resolve_plan_check_route,
    token_claims,
)
from core.payment_method_detector import (
    classify_payment_method,
    parse_capability_evidence,
    parse_checkout_session,
)
from core.session import BrowserSession

CHECKOUT_PATH = "/backend-api/payments/checkout"
CHECKOUT_URL = f"https://chatgpt.com{CHECKOUT_PATH}"
STRIPE_INIT_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}/init"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)


class GcashEligibilityError(RuntimeError):
    """带有安全 HTTP 状态信息的探测异常。"""

    def __init__(self, message: str, *, http_status: int | None = None, kind: str = "unknown"):
        super().__init__(message)
        self.http_status = http_status
        self.kind = kind


def _setting(name: str, fallback: Any) -> Any:
    try:
        from config import gcash as gcash_cfg

        return getattr(gcash_cfg, name, fallback)
    except Exception:
        return fallback


def safe_gcash_log_text(value: object, limit: int = 240) -> str:
    """压缩并移除 Token、代理认证、Checkout ID 和 Stripe key。"""
    text = str(value or "")
    text = re.sub(r"https?://[^\s]+", _safe_url, text)
    text = re.sub(
        r"(?i)\b(?:eyJ[a-zA-Z0-9_-]+\.){2}[a-zA-Z0-9_-]+\b",
        "<jwt-redacted>",
        text,
    )
    text = re.sub(r"(?i)\b(?:cs|oaics)_[A-Za-z0-9_-]+\b", "<checkout-redacted>", text)
    text = re.sub(r"(?i)\bpk_(?:live|test)_[A-Za-z0-9_-]+\b", "<stripe-key-redacted>", text)
    text = re.sub(
        r"(?i)(authorization|access[_-]?token|refresh[_-]?token|cookie|secret|password|passwd|token)\s*[:=]\s*[^,\s}]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1***:***@", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(1, int(limit))]


def _safe_url(match) -> str:
    try:
        parsed = urlparse(match.group(0))
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path or ''}"
    except Exception:
        return "<url>"


def format_gcash_phase(phase: str, **fields: Any) -> str:
    """生成只包含派生字段的 GCash 阶段日志。"""
    parts = [f"[GCash] phase={str(phase or 'unknown').strip() or 'unknown'}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        rendered = "true" if value is True else "false" if value is False else safe_gcash_log_text(value)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


def _checkout_body(trial_days: int) -> dict[str, Any]:
    country = str(_setting("GCASH_CHECK_COUNTRY", "PH") or "PH").strip().upper()
    currency = str(_setting("GCASH_CHECK_CURRENCY", "PHP") or "PHP").strip().upper()
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
    }
    if int(trial_days or 0) > 0:
        body["subscription_data"] = {"trial_period_days": int(trial_days)}
    return body


def _chatgpt_headers(env: BrowserSession, token: str) -> dict[str, str]:
    headers = env.get_chatgpt_headers(referer="https://chatgpt.com/")
    headers.update(
        {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {normalize_token(token)}",
            "referer": "https://chatgpt.com/",
            "x-openai-target-path": CHECKOUT_PATH,
            "x-openai-target-route": CHECKOUT_PATH,
        }
    )
    return headers


def _stripe_headers() -> dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://checkout.stripe.com",
        "referer": "https://checkout.stripe.com/",
    }


def _response_json(response: Any, *, stage: str) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:
        raise GcashEligibilityError(
            f"{stage} response is not JSON ({type(exc).__name__})",
            http_status=int(getattr(response, "status_code", 0) or 0) or None,
        ) from exc
    if not isinstance(data, dict):
        raise GcashEligibilityError(f"{stage} response is not an object", http_status=int(getattr(response, "status_code", 0) or 0) or None)
    return data


def _checkout_summary(data: dict[str, Any], http_status: int) -> dict[str, Any]:
    session_id = str(
        data.get("checkout_session_id")
        or data.get("session_id")
        or data.get("id")
        or ""
    ).strip()
    if not session_id:
        raise GcashEligibilityError("checkout response missing session", http_status=http_status)
    raw_key = (
        data.get("stripe_publishable_key")
        or data.get("publishable_key")
        or data.get("publishableKey")
        or data.get("stripePublishableKey")
        or data.get("key")
        or ""
    )
    key_match = re.search(r"\bpk_(?:live|test)_[A-Za-z0-9]+\b", str(raw_key))
    if not key_match:
        raise GcashEligibilityError("checkout response missing publishable key", http_status=http_status)

    checkout_session = data.get("checkout_session")
    checkout_summary: dict[str, Any] = {}
    if isinstance(checkout_session, dict):
        subscription_data = checkout_session.get("subscription_data")
        if isinstance(subscription_data, dict):
            checkout_summary["subscription_data"] = {
                key: subscription_data[key]
                for key in ("trial_period_days", "trial_end")
                if key in subscription_data
            }
        for key in ("trial_period_days", "trial_end", "mode"):
            if key in checkout_session:
                checkout_summary[key] = checkout_session[key]
    for key in ("trial_period_days", "trial_end"):
        if key in data:
            checkout_summary[key] = data[key]

    return {
        "checkout_session_id": session_id,
        "one_click_trial_eligible": data.get("one_click_trial_eligible"),
        "is_new_stripe_customer": data.get("is_new_stripe_customer"),
        "checkout_session": checkout_summary,
        "_http_status": http_status,
        "_publishable_key": key_match.group(0),
    }


def _is_already_paid(value: object) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "user is already paid",
            "already_subscribed",
            "already subscribed",
            "active subscription",
        )
    )


def _checkout_session(
    session: BrowserSession,
    token: str,
    *,
    trial_days: int,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """创建未确认的 Checkout Session，并返回最小化响应摘要和 publishable key。"""
    response = session.post(
        CHECKOUT_URL,
        json=_checkout_body(trial_days),
        headers=_chatgpt_headers(session, token),
        timeout=float(timeout),
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = getattr(response, "text", "")
        if status == 401:
            kind = "credential_invalid"
        elif _is_already_paid(text):
            kind = "already_paid"
        else:
            kind = "unknown"
        raise GcashEligibilityError(f"checkout HTTP {status}", http_status=status, kind=kind)
    data = _response_json(response, stage="checkout")
    summary = _checkout_summary(data, status)
    return summary, str(summary["_publishable_key"])


def _stripe_init(
    session: BrowserSession,
    checkout_session_id: str,
    publishable_key: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    """读取 Stripe payment page 初始化信息，不创建 PaymentMethod。"""
    if not checkout_session_id or not publishable_key:
        raise GcashEligibilityError("Stripe init parameters are incomplete")
    stripe_js_id = f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"
    body = {
        "browser_locale": "en-US",
        "browser_timezone": "Asia/Manila",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en-US",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": publishable_key,
        "_stripe_version": STRIPE_VERSION,
    }
    url = STRIPE_INIT_URL.format(checkout_session_id=checkout_session_id)
    response = session.post(url, data=body, headers=_stripe_headers(), timeout=float(timeout))
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise GcashEligibilityError(f"stripe init HTTP {status}", http_status=status)
    data = _response_json(response, stage="stripe_init")
    data["_http_status"] = status
    return data


def _has_actual_trial(value: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, dict):
        for key in ("trial_period_days", "trial_days"):
            candidate = value.get(key)
            try:
                if int(candidate or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        for key in ("trial_end", "trial_end_at"):
            if value.get(key) not in (None, "", 0, "0", False):
                return True
        return any(_has_actual_trial(item, depth=depth + 1) for item in value.values() if isinstance(item, (dict, list)))
    if isinstance(value, list):
        return any(_has_actual_trial(item, depth=depth + 1) for item in value if isinstance(item, (dict, list)))
    return False


def _base_result(route: dict[str, Any], *, max_attempts: int, currency: str = "PHP") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "conclusive": False,
        "decision": "unknown",
        "gcash_available": None,
        "trial_eligible": None,
        "actual_trial": False,
        "payment_methods": [],
        "payment_method_status": "unknown",
        "currency": currency,
        "amount_due": None,
        "stripe_mode": None,
        "http_status": None,
        "error": None,
        "network_route": route.get("network_route"),
        "proxy_used": route.get("proxy_used"),
        "proxy_ip": None,
        "attempt_count": 0,
        "max_attempts": max_attempts,
        "retryable": True,
    }
    result.update(_proxy_log_fields(str(route.get("proxy") or "")))
    return result


def _emit(callback: Callable[[str], Any] | None, line: str) -> None:
    if callback is None:
        return
    try:
        callback(safe_gcash_log_text(line))
    except Exception:
        return


def check_account_gcash(
    token: str,
    *,
    proxy: str | None = None,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    trial_days: int = 0,
    progress_callback: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """查询 GCash 支付方式与试用字段，永不执行付款确认。"""
    attempts_limit = max(1, int(max_attempts if max_attempts is not None else _setting("GCASH_CHECK_MAX_ATTEMPTS", 2)))
    request_timeout = float(timeout if timeout is not None else _setting("GCASH_CHECK_TIMEOUT", 20.0))
    delay = max(0.0, float(retry_delay if retry_delay is not None else _setting("GCASH_CHECK_RETRY_DELAY", 2.0)))
    route = resolve_plan_check_route(proxy)
    country = str(_setting("GCASH_CHECK_COUNTRY", "PH") or "PH").strip().upper()
    currency = str(_setting("GCASH_CHECK_CURRENCY", "PHP") or "PHP").strip().upper()
    result = _base_result(route, max_attempts=attempts_limit, currency=currency)
    _emit(
        progress_callback,
        format_gcash_phase(
            "route",
            network_route=result.get("network_route"),
            proxy_used=result.get("proxy_used"),
            **{key: result[key] for key in ("proxy_ip", "proxy_port") if key in result},
        ),
    )

    normalized = normalize_token(token)
    if not normalized:
        result.update({"decision": "credential_invalid", "conclusive": True, "retryable": False, "error": "empty credential"})
        return result
    claims = token_claims(normalized)
    if claims.get("token_expired") is True:
        result.update({"decision": "credential_invalid", "conclusive": True, "retryable": False, "error": "expired credential"})
        return result

    for attempt in range(1, attempts_limit + 1):
        result["attempt_count"] = attempt
        env: BrowserSession | None = None
        try:
            env = BrowserSession(proxy=route.get("proxy", ""), detect_exit_geo=False)
            _emit(progress_callback, format_gcash_phase("checkout_request", attempt=attempt, country=country, currency=currency))
            checkout_payload, publishable_key = _checkout_session(
                env,
                normalized,
                trial_days=int(trial_days or 0),
                timeout=request_timeout,
            )
            checkout_status = checkout_payload.get("_http_status") if isinstance(checkout_payload, dict) else None
            result["http_status"] = checkout_status or result.get("http_status")
            eligible = checkout_payload.get("one_click_trial_eligible")
            result["trial_eligible"] = eligible if isinstance(eligible, bool) else None
            _emit(
                progress_callback,
                format_gcash_phase(
                    "checkout_response",
                    http_status=checkout_status,
                    trial_eligible=result.get("trial_eligible"),
                ),
            )
            session_info = parse_checkout_session(
                checkout_payload,
                billing_country=country,
                fallback_publishable_key=publishable_key,
            )
            _emit(progress_callback, format_gcash_phase("stripe_init", status="requesting"))
            stripe_payload = _stripe_init(
                env,
                session_info.checkout_session_id,
                session_info.publishable_key or publishable_key,
                timeout=request_timeout,
            )
            result["http_status"] = stripe_payload.get("_http_status") or result.get("http_status")
            evidence = parse_capability_evidence(stripe_payload, fallback_currency=currency)
            method_status, method_available = classify_payment_method(evidence, "gcash")
            result.update(
                {
                    "ok": method_status != "unknown",
                    "conclusive": method_status != "unknown",
                    "gcash_available": method_available,
                    "payment_methods": list(evidence.payment_method_types),
                    "payment_method_status": method_status,
                    "currency": evidence.currency or currency,
                    "amount_due": evidence.amount_minor,
                    "stripe_mode": stripe_payload.get("mode") or (stripe_payload.get("elements_options") or {}).get("mode"),
                    "actual_trial": _has_actual_trial(checkout_payload) or _has_actual_trial(stripe_payload),
                    "error": None,
                    "retryable": False,
                }
            )
            if (
                result["trial_eligible"] is False
                and int(trial_days or 0) > 0
                and not result["actual_trial"]
            ):
                result["decision"] = "trial_ineligible"
            else:
                result["decision"] = method_status
            _emit(
                progress_callback,
                format_gcash_phase(
                    "result",
                    decision=result["decision"],
                    gcash_available=result["gcash_available"],
                    trial_eligible=result["trial_eligible"],
                    actual_trial=result["actual_trial"],
                    payment_methods=result["payment_methods"],
                    currency=result["currency"],
                    amount_due=result["amount_due"],
                    stripe_mode=result["stripe_mode"],
                    http_status=result["http_status"],
                ),
            )
            return result
        except GcashEligibilityError as exc:
            result["http_status"] = exc.http_status or result.get("http_status")
            if exc.kind == "credential_invalid":
                result.update({"decision": "credential_invalid", "conclusive": True, "retryable": False, "error": "credential rejected"})
                return result
            if exc.kind == "already_paid":
                result.update({"decision": "already_paid", "conclusive": True, "retryable": False, "error": "account already has an active subscription"})
                return result
            result["error"] = safe_gcash_log_text(str(exc))
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {safe_gcash_log_text(str(exc))}"
        finally:
            env = None
        if attempt < attempts_limit and delay:
            _emit(progress_callback, format_gcash_phase("retry", attempt=attempt, delay_seconds=delay, error=result.get("error")))
            time.sleep(delay)

    result["decision"] = "unknown"
    result["gcash_available"] = None
    result["payment_method_status"] = "unknown"
    result["conclusive"] = False
    result["ok"] = False
    result["retryable"] = True
    _emit(progress_callback, format_gcash_phase("result", decision="unknown", error=result.get("error"), http_status=result.get("http_status")))
    return result
