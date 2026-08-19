# -*- coding: utf-8 -*-
"""在已登录的 Roxy/Selenium 浏览器会话内补设 TOTP 2FA。"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode

import pyotp

from core.email_provider import capture_otp_baseline, wait_for_otp


_SIX_DIGIT = re.compile(r"(?<!\d)\d{6}(?!\d)")


class TwoFAAlreadyEnabledExternal(RuntimeError):
    """平台已启用 MFA，但本地没有可恢复的 TOTP Secret。"""


class TwoFAEnrollmentUncertain(RuntimeError):
    """enroll 后激活结果不明确，禁止自动重新 enroll。"""


def redact_twofa_error(value) -> str:
    text = _SIX_DIGIT.sub("<otp-redacted>", str(value or ""))
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]+", "Bearer <redacted>", text)
    text = re.sub(r"(?i)(secret[\"'\s:=]+)[A-Z2-7]{16,}", r"\1<redacted>", text)
    return text[:500]


def _ensure_chatgpt(driver) -> None:
    from core.roxy_registration import _safe_get

    if "chatgpt.com" not in str(getattr(driver, "current_url", "") or "").lower():
        _safe_get(driver, "https://chatgpt.com/", timeout=45, attempts=2, accept_hosts=("chatgpt.com",))


def _device_id(driver) -> str:
    try:
        value = driver.execute_script(
            "return window.localStorage.getItem('oaicom_stable_id') || "
            "document.cookie.match(/(?:^|; )oai-did=([^;]+)/)?.[1] || '';"
        )
        return str(value or "").strip()
    except Exception:
        return ""


def _fetch_reauth_authorize_url(driver, email: str) -> str:
    _ensure_chatgpt(driver)
    csrf_result = driver.execute_async_script(r"""
    const done = arguments[arguments.length - 1];
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    fetch('/api/auth/csrf', {credentials:'include', headers:{accept:'*/*'}, signal:controller.signal})
      .then(async r => { const text=await r.text(); let data={}; try{data=JSON.parse(text)}catch(_){}
        done({ok:r.ok,status:r.status,data}); })
      .catch(e => done({ok:false,error:String(e)})).finally(() => clearTimeout(timer));
    """) or {}
    data = csrf_result.get("data") if isinstance(csrf_result, dict) else None
    csrf = data.get("csrfToken") if isinstance(data, dict) else None
    if not csrf:
        raise RuntimeError(f"2FA 获取 CSRF 失败: {redact_twofa_error(csrf_result)}")

    query = {
        "connection": "password",
        "login_hint": str(email or "").strip(),
        "reauth": "password",
        "max_age": "0",
        "ext-oai-did": _device_id(driver),
    }
    signin_url = "https://chatgpt.com/api/auth/signin/openai?" + urlencode(query)
    body = urlencode({
        "callbackUrl": "https://chatgpt.com/?action=enable&factor=totp",
        "csrfToken": csrf,
        "json": "true",
    })
    result = driver.execute_async_script(r"""
    const url=arguments[0], body=arguments[1], done=arguments[arguments.length-1];
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),15000);
    fetch(url,{method:'POST',credentials:'include',headers:{'content-type':'application/x-www-form-urlencoded',accept:'*/*'},body,signal:controller.signal})
      .then(async r=>{const text=await r.text();let data={};try{data=JSON.parse(text)}catch(_){}
        done({ok:r.ok,status:r.status,data});})
      .catch(e=>done({ok:false,error:String(e)})).finally(()=>clearTimeout(timer));
    """, signin_url, body) or {}
    result_data = result.get("data") if isinstance(result, dict) else None
    authorize_url = result_data.get("url") if isinstance(result_data, dict) else None
    if not authorize_url:
        raise RuntimeError(f"2FA 未获取 authorize URL: {redact_twofa_error(result)}")
    return str(authorize_url)


def _complete_reauth_otp(driver, email: str, authorize_url: str, *, progress) -> None:
    from core.roxy_registration import (
        _clear_otp_inputs,
        _click_continue,
        _click_resend_email_otp,
        _is_email_verification_page,
        _safe_get,
        _type_otp,
        _wait_after_email_otp_submit,
    )

    baseline = capture_otp_baseline(email)
    after_ts = time.time()
    _safe_get(
        driver, authorize_url, timeout=60, attempts=2,
        accept_hosts=("auth.openai.com", "chatgpt.com"),
    )
    if not _is_email_verification_page(driver):
        raise RuntimeError("2FA 重认证未进入邮箱验证码页面")
    for attempt in range(1, 4):
        progress(f"[2FA] 等待重认证邮箱 OTP attempt={attempt}/3")
        code = wait_for_otp(email, after_ts=after_ts, otp_baseline=baseline)
        _clear_otp_inputs(driver)
        _type_otp(driver, code)
        try:
            _click_continue(driver)
        except Exception:
            pass
        if _wait_after_email_otp_submit(driver, timeout=12) == "accepted":
            return
        if attempt >= 3:
            raise RuntimeError("2FA 重认证邮箱验证码连续无效或过期")
        baseline = capture_otp_baseline(email)
        after_ts = time.time()
        _click_resend_email_otp(driver, timeout=25)


def _mfa_request(driver, path: str, access_token: str, payload: dict) -> dict:
    _ensure_chatgpt(driver)
    result = driver.execute_async_script(r"""
    const path=arguments[0], token=arguments[1], payload=arguments[2], done=arguments[arguments.length-1];
    const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),20000);
    fetch(path,{method:'POST',credentials:'include',headers:{
      'content-type':'application/json','authorization':'Bearer '+token,'accept':'application/json'
    },body:JSON.stringify(payload),signal:controller.signal})
      .then(async r=>{const text=await r.text();let data={};try{data=JSON.parse(text)}catch(_){}
        done({ok:r.ok,status:r.status,data});})
      .catch(e=>done({ok:false,error:String(e)})).finally(()=>clearTimeout(timer));
    """, path, access_token, payload) or {}
    if not isinstance(result, dict):
        raise RuntimeError("2FA 浏览器请求返回格式异常")
    return result


def setup_existing_account_2fa(driver, email: str, *, progress_callback=None) -> dict:
    """当前浏览器已登录账号时，二次邮箱认证并启用 TOTP。"""
    from core.roxy_registration import _fetch_chatgpt_session

    progress = progress_callback or (lambda _message: None)
    progress("[2FA] 发起安全设置重认证")
    authorize_url = _fetch_reauth_authorize_url(driver, email)
    _complete_reauth_otp(driver, email, authorize_url, progress=progress)
    session = _fetch_chatgpt_session(driver, timeout=90, auto_jump_wait=5)
    token = str(session.get("accessToken") or "")
    if not token:
        raise RuntimeError("2FA 重认证完成但未获取新的 accessToken")

    progress("[2FA] 创建 TOTP enrollment")
    enroll = _mfa_request(driver, "/backend-api/accounts/mfa/enroll", token, {"factor_type": "totp"})
    enroll_data = enroll.get("data") if isinstance(enroll.get("data"), dict) else {}
    secret = str(enroll_data.get("secret") or "")
    session_id = str(enroll_data.get("session_id") or "")
    if not enroll.get("ok") or not secret or not session_id:
        raise RuntimeError(f"2FA enroll 失败 status={enroll.get('status')}")

    progress("[2FA] 激活 TOTP enrollment")
    last = None
    for attempt in range(2):
        code = pyotp.TOTP(secret).now()
        last = _mfa_request(driver, "/backend-api/accounts/mfa/user/activate_enrollment", token, {
            "code": code,
            "factor_type": "totp",
            "session_id": session_id,
        })
        data = last.get("data") if isinstance(last.get("data"), dict) else {}
        if last.get("ok") and data.get("success"):
            progress("[2FA] TOTP 已激活")
            return {
                "ok": True,
                "totp_secret": secret,
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
        if attempt == 0 and last.get("status") in (400, 409, 422):
            time.sleep(1)
            continue
        break
    raise TwoFAEnrollmentUncertain(
        f"2FA activate 结果不明确 status={(last or {}).get('status')}，已停止自动重试"
    )
