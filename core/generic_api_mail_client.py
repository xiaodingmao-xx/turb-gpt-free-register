# -*- coding: utf-8 -*-
"""
通用 API 取码邮箱客户端。

邮箱池导入格式：
    email----code_url

注册时领取 email；取码时直接 GET code_url，并从响应中提取 6 位验证码。
响应可以是纯文本、HTML 或 JSON，只要其中包含 6 位验证码即可。
"""
import json
import logging
import re
import time
import base64
import hashlib
import html as html_lib
from datetime import datetime
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlunparse

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)

_CODE_REGEX = re.compile(r"\b(\d{6})\b")
# 仅用于识别 HTML 响应，真正提取时必须先转换为用户可见文本。
_HTML_MARKER_RE = re.compile(
    r"<(?:!doctype|html|head|body|style|script|table|div|p|span|br|strong)\b",
    re.IGNORECASE,
)
_CONTEXT_WORDS = ("code", "verify", "verification", "验证码", "代码", "确认码", "認証", "コード")
_CONTEXT_CACHE: dict[str, "GenericApiEmailAccount"] = {}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNTS_FILE = _PROJECT_ROOT / "用于注册的API邮箱.txt"
_YANGYANG_MESSAGES_RE = re.compile(r"/messages/([^/]+)/([^/?#]+)", re.IGNORECASE)
_YANGYANG_OPENAI_SUBJECT_HINTS = (
    "temporary chatgpt",
    "chatgpt verification code",
    "chatgpt login code",
    "临时 chatgpt",
    "chatgpt 登录代码",
    "chatgpt 验证码",
    "一時的な認証コード",
    "一時ログインコード",
)


class GenericApiMailError(RuntimeError):
    """通用 API 取码邮箱错误。"""


@dataclass
class GenericApiEmailAccount:
    email: str
    code_url: str


@dataclass(frozen=True)
class GenericOtpObservation:
    code: str | None
    source: str
    received_at: object | None
    msg_ts: float | None
    message_id: str | None
    structured: bool
    rejection_reason: str | None = None
    subject: str | None = None


def _flatten_json(obj) -> str:
    parts: list[str] = []
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            parts.append(str(x))
    walk(obj)
    return "\n".join(parts)


def _decode_data_uri(text: str) -> str:
    """把 data:text/html;base64,... 正文解码成可抽取 OTP 的 HTML/文本。"""
    if not isinstance(text, str):
        return ""
    if not text.startswith("data:"):
        return text
    try:
        _meta, payload = text.split(",", 1)
    except ValueError:
        return text
    if ";base64" in _meta.lower():
        try:
            return base64.b64decode(payload).decode("utf-8", errors="replace")
        except Exception:
            return text
    try:
        from urllib.parse import unquote_to_bytes
        return unquote_to_bytes(payload).decode("utf-8", errors="replace")
    except Exception:
        return text


def _html_to_visible_text(text: str) -> str:
    """删除 HTML/CSS/脚本和标签属性，只保留用户可见文本。"""
    body = _decode_data_uri(text or "")
    body = re.sub(
        r"<(style|script|head)\b[^>]*>.*?</\1\s*>",
        " ",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_lib.unescape(body)
    return re.sub(r"\s+", " ", body).strip()


def _extract_code(text: str, *, is_html: bool | None = None) -> str | None:
    """从纯文本或 HTML 可见正文中提取可信的 6 位 OTP。"""
    body = _decode_data_uri(text or "").strip()
    if not body:
        return None

    html_input = bool(_HTML_MARKER_RE.search(body)) if is_html is None else is_html
    searchable = _html_to_visible_text(body) if html_input else html_lib.unescape(body)
    searchable = searchable.strip()
    if re.fullmatch(r"\d{6}", searchable):
        return searchable

    lower = searchable.lower()
    for match in _CODE_REGEX.finditer(searchable):
        window = lower[
            max(0, match.start() - 80):
            min(len(lower), match.end() + 80)
        ]
        if any(word.lower() in window for word in _CONTEXT_WORDS):
            return match.group(1)
    return None


def _extract_yangyang_openai_code(subject: str, body: str) -> str | None:
    """
    yangyang 邮件详情里 OpenAI 模板常混入多个 6 位数字：
    - 202123 / 353740 这类 CSS/模板数字
    - 真正 OTP 在 “Your code is / code:” 附近，通常是正文最后一个业务 6 位数
    所以不能直接复用通用 _extract_code 的“第一个上下文命中”。
    """
    body = _decode_data_uri(body or "")
    subject_l = (subject or "").lower()
    text = "\n".join([subject or "", body])

    # 去掉 style/script，减少 CSS 颜色、宽高等 6 位数字干扰。
    clean = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<script[^>]*>.*?</script>", " ", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"#[0-9a-fA-F]{6}\b", " ", clean)
    clean = re.sub(r"(?:color|background|border|width|height|font-size|line-height)\s*:\s*[^;\"']+", " ", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    codes = _CODE_REGEX.findall(clean)
    if not codes:
        return None

    # 过滤已知模板噪声；保留其它 6 位候选。
    noise = {"000000", "202123", "353740"}
    candidates = [c for c in codes if c not in noise]
    if not candidates:
        return None

    lower = clean.lower()
    patterns = (
        r"(?:code is|code:|verification code is|login code is|your code is)\D{0,80}(\d{6})",
        r"(?:验证码|驗證碼|登录代码|登入代碼|確認コード|認証コード|ログインコード)\D{0,80}(\d{6})",
        r"(\d{6})\D{0,80}(?:code|验证码|驗證碼|確認コード|認証コード)",
    )
    for pat in patterns:
        matches = re.findall(pat, clean, flags=re.IGNORECASE)
        matches = [m for m in matches if m not in noise]
        if matches:
            return matches[-1]

    # OpenAI 临时代码邮件：清理噪声后最后一个业务 6 位数最稳定。
    if any(h in subject_l for h in _YANGYANG_OPENAI_SUBJECT_HINTS) or "openai" in lower or "chatgpt" in lower:
        return candidates[-1]

    fallback = _extract_code(clean)
    return fallback if fallback in candidates else None


def _parse_yangyang_code_url(code_url: str) -> tuple[str, str, str] | None:
    """
    解析 yangyang.website 这类邮箱页面：
        /messages/{token}/{email}
    返回 (origin, token, email)。
    """
    try:
        parsed = urlparse(code_url)
    except Exception:
        return None
    m = _YANGYANG_MESSAGES_RE.search(parsed.path or "")
    if not m:
        return None
    origin = urlunparse((parsed.scheme or "http", parsed.netloc, "", "", "", ""))
    token = unquote(m.group(1))
    email = unquote(m.group(2))
    if not origin or not token or not email:
        return None
    return origin.rstrip("/"), token, email


def _parse_yangyang_ts(value: str | None) -> float | None:
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def _parse_generic_api_ts(value) -> float | None:
    """解析通用 API 返回的时间字段，兼容 ISO8601/Z 和常见本地时间格式。"""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # 数字时间戳：秒 / 毫秒
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except Exception:
            return None
    # ISO8601: 2026-08-05T01:10:17.000Z
    try:
        iso = raw
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        pass
    # 常见字符串格式
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def _extract_code_from_structured_dict(data: dict) -> tuple[str | None, str]:
    """从已解析 JSON 中提取验证码，不负责判断邮件新鲜度。"""
    raw_code = next(
        (
            data.get(name)
            for name in (
                "code",
                "otp",
                "verification_code",
                "verificationCode",
                "email_code",
                "emailCode",
            )
            if data.get(name) is not None
        ),
        None,
    )
    if raw_code is not None:
        value = str(raw_code).strip()
        if re.fullmatch(r"\d{6}", value):
            return value, "json_code_field"
        return None, "json_code_field"
    else:
        message = next(
            (
                data.get(name)
                for name in ("message", "text", "content", "html", "body")
                if isinstance(data.get(name), str) and data.get(name).strip()
            ),
            "",
        )
        decoded_message = _decode_data_uri(message)
        is_html = bool(_HTML_MARKER_RE.search(decoded_message))
        code = _extract_code(message, is_html=is_html)
        return code, "html_visible_text" if is_html else "plain_text"


def _parse_generic_api_observation(
    text: str,
    after_ts: float | None = None,
    max_age_seconds: int | None = None,
    now_ts: float | None = None,
) -> GenericOtpObservation:
    """解析 GenericAPI 响应，并保留结构化响应的拒绝原因。"""
    if not text:
        return GenericOtpObservation(None, "plain_text", None, None, None, False)
    try:
        data = json.loads(text)
    except Exception:
        return GenericOtpObservation(None, "plain_text", None, None, None, False)
    if not isinstance(data, dict):
        return GenericOtpObservation(None, "structured_api", None, None, None, True, "invalid_shape")

    nested_message = data.get("message")
    payload = nested_message if isinstance(nested_message, dict) else data

    ts_raw = None
    msg_ts = None
    time_sources = (payload, data) if payload is not data else (data,)
    for time_source in time_sources:
        for name in (
            "timestamp",
            "time",
            "received_at",
            "receivedAt",
            "created_at",
            "createdAt",
            "date",
        ):
            candidate = time_source.get(name)
            if candidate is None or str(candidate).strip() == "":
                continue
            parsed_ts = _parse_generic_api_ts(candidate)
            if ts_raw is None:
                ts_raw = candidate
            if parsed_ts is not None:
                ts_raw = candidate
                msg_ts = parsed_ts
                break
        if msg_ts is not None:
            break

    message_id = (
        payload.get("message_id")
        or payload.get("messageId")
        or payload.get("id")
        or payload.get("uid")
        or data.get("message_id")
        or data.get("messageId")
        or data.get("id")
    )
    code, source = _extract_code_from_structured_dict(payload)
    rejection_reason = None
    current_ts = time.time() if now_ts is None else now_ts

    if data.get("ok") is False or data.get("found") is False:
        code, rejection_reason = None, "not_found"
    elif code and after_ts is not None and msg_ts is not None and msg_ts + 2 < after_ts:
        rejection_reason = "before_trigger"
        code = None
    elif (
        code
        and max_age_seconds is not None
        and max_age_seconds > 0
        and msg_ts is not None
        and current_ts - msg_ts > max_age_seconds
    ):
        rejection_reason = "older_than_max_age"
        code = None

    return GenericOtpObservation(
        code=code,
        source=source or "structured_api",
        received_at=ts_raw,
        msg_ts=msg_ts,
        message_id=str(message_id) if message_id is not None else None,
        structured=True,
        rejection_reason=rejection_reason,
        subject=str(payload.get("subject") or data.get("subject") or "") or None,
    )


def _extract_structured_api_code(text: str, after_ts: float | None = None) -> tuple[str, dict] | None:
    """兼容旧调用方的结构化验证码提取包装层。"""
    observation = _parse_generic_api_observation(text, after_ts=after_ts)
    if not observation.code:
        return None

    try:
        data = json.loads(text)
    except Exception:
        data = {}
    payload = (
        data.get("message")
        if isinstance(data, dict) and isinstance(data.get("message"), dict)
        else data
    )
    return observation.code, {
        "source": observation.source,
        "received_at": observation.received_at,
        "msg_ts": observation.msg_ts,
        "message_id": observation.message_id,
        "subject": observation.subject,
        "from": (
            payload.get("from") or payload.get("fromAddress") or payload.get("sender")
            if isinstance(payload, dict) else None
        ),
    }


def _fetch_yangyang_otp(
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    baseline_message_ids: frozenset[str] | None = None,
) -> tuple[str, dict] | None:
    """从 yangyang 邮箱页面的列表 API + 详情 API 中抽取最新 6 位验证码。"""
    parsed = _parse_yangyang_code_url(code_url)
    if not parsed:
        return None
    origin, token, email = parsed
    token_q = quote(token, safe="")
    email_q = quote(email, safe="@._+-")
    api_url = f"{origin}/api/messages/{token_q}/{email_q}"

    items: list[dict] = []
    cursor: str | None = None
    # 一般第一页足够；保守支持最多翻 5 页。
    for _ in range(5):
        url = api_url if not cursor else f"{api_url}?cursor={quote(str(cursor), safe='')}"
        resp = session.get(url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
        if resp.status_code != 200:
            if resp.status_code == 404:
                # 兼容 mail.ai1998.xyz 这类同样是 /messages/{token}/{email}，
                # 但没有 /api/messages，邮件直接内嵌在 HTML 页面中的实现。
                return _fetch_inline_messages_page_otp(
                    session=session,
                    code_url=code_url,
                    headers=headers,
                    after_ts=after_ts,
                    baseline_message_ids=baseline_message_ids,
                )
            response_body = resp.text or ""
            logger.debug(
                "[GenericAPI] yangyang 邮件列表 HTTP %s: has_body=%s body_len=%s",
                resp.status_code,
                bool(response_body),
                len(response_body),
            )
            return None
        data = resp.json()
        page_items = data.get("items") or []
        if isinstance(page_items, list):
            items.extend([x for x in page_items if isinstance(x, dict)])
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = str(data.get("next_cursor"))

    # API 默认新邮件在前；再次按时间倒序，尽量取最新验证码。
    items.sort(key=lambda x: _parse_yangyang_ts(x.get("received_at") or x.get("receivedAt")) or 0, reverse=True)
    baseline_ids = {str(value) for value in (baseline_message_ids or ())}
    for item in items:
        msg_id = item.get("id")
        if msg_id is not None and str(msg_id) in baseline_ids:
            logger.debug("[GenericAPI] yangyang 跳过基线邮件: id=%s", msg_id)
            continue
        msg_ts_raw = item.get("received_at") or item.get("receivedAt")
        msg_ts = _parse_yangyang_ts(msg_ts_raw)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] yangyang 跳过旧邮件: id=%s ts=%s after=%s has_subject=%s subject_len=%s",
                item.get("id"), msg_ts_raw, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                bool(item.get("subject")), len(str(item.get("subject") or "")),
            )
            continue
        if not msg_id:
            continue
        detail_url = f"{origin}/message/{quote(str(msg_id), safe='')}/{token_q}/{email_q}"
        try:
            detail_resp = session.get(detail_url, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()
        except Exception as exc:
            logger.debug(f"[GenericAPI] yangyang 邮件详情读取失败: {type(exc).__name__}: {exc}")
            continue

        raw_body = str(detail.get("body") or "")
        body = _decode_data_uri(raw_body)
        subject = str(detail.get("subject") or item.get("subject") or "")
        text = "\n".join([
            subject,
            str(detail.get("fromAddress") or item.get("from_address") or ""),
            str(detail.get("receivedAt") or item.get("received_at") or ""),
            body,
        ])
        code = _extract_yangyang_openai_code(subject, body)
        if code:
            logger.info(
                f"[GenericAPI] yangyang 页面提取到 OTP 候选, "
                f"mail_id={msg_id}, ts={detail.get('receivedAt') or item.get('received_at')}, "
                f"has_subject={bool(subject)}, subject_len={len(subject)}"
            )
            return code, {
                "mail_id": msg_id,
                "received_at": detail.get("receivedAt") or item.get("received_at"),
                "subject": subject,
                "msg_ts": msg_ts,
            }
    return None


def _strip_html_fragment(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def _fetch_inline_messages_page_otp(
    *,
    session: requests.Session,
    code_url: str,
    headers: dict,
    after_ts: float | None = None,
    baseline_message_ids: frozenset[str] | None = None,
) -> tuple[str, dict] | None:
    """解析无 JSON API、直接把邮件卡片渲染在 HTML 里的 /messages 页面。"""
    try:
        resp = session.get(
            code_url,
            headers={**headers, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
            timeout=20,
            verify=False,
        )
        if resp.status_code != 200:
            response_body = resp.text or ""
            logger.debug(
                "[GenericAPI] inline messages 页面 HTTP %s: has_body=%s body_len=%s",
                resp.status_code,
                bool(response_body),
                len(response_body),
            )
            return None
        html = resp.text or ""
    except Exception as exc:
        logger.debug("[GenericAPI] inline messages 页面读取失败: %s: %s", type(exc).__name__, exc)
        return None

    cards: list[tuple[str, str]] = []
    for match in re.finditer(
        r"<article\b(?P<attrs>[^>]*)>(?P<body>.*?)</article>",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        attrs = match.group("attrs") or ""
        class_match = re.search(r"\bclass\s*=\s*[\"']([^\"']*)[\"']", attrs, flags=re.IGNORECASE)
        if class_match and "mail-card" in class_match.group(1).lower().split():
            cards.append((attrs, match.group("body")))
    # 没有 article 时退一步按 details 分块，避免 class 名细微变化。
    if not cards:
        cards = [
            (match.group("attrs") or "", match.group("body"))
            for match in re.finditer(
                r"<details\b(?P<attrs>[^>]*)>(?P<body>.*?)</details>",
                html,
                flags=re.DOTALL | re.IGNORECASE,
            )
        ]

    items: list[dict] = []
    for attrs, card in cards:
        attr_map = {
            name.lower(): html_lib.unescape(value).strip()
            for name, value in re.findall(
                r"([:\w-]+)\s*=\s*[\"']([^\"']*)[\"']",
                attrs,
                flags=re.IGNORECASE,
            )
        }
        subject_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*subject[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
        date_m = re.search(r"<span\b[^>]*class=[\"'][^\"']*date[^\"']*[\"'][^>]*>(.*?)</span>", card, flags=re.DOTALL | re.IGNORECASE)
        from_m = re.search(r"<div\b[^>]*class=[\"'][^\"']*meta[^\"']*[\"'][^>]*>(.*?)</div>", card, flags=re.DOTALL | re.IGNORECASE)
        body_m = re.search(r"<pre\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</pre>", card, flags=re.DOTALL | re.IGNORECASE)
        if not body_m:
            body_m = re.search(r"<div\b[^>]*class=[\"'][^\"']*body[^\"']*[\"'][^>]*>(.*?)</div>", card, flags=re.DOTALL | re.IGNORECASE)

        subject = _strip_html_fragment(subject_m.group(1) if subject_m else "")
        received_at = _strip_html_fragment(date_m.group(1) if date_m else "")
        from_addr = _strip_html_fragment(from_m.group(1) if from_m else "")
        body = _strip_html_fragment(body_m.group(1) if body_m else card)
        msg_ts = _parse_yangyang_ts(received_at)
        mail_id = next((
            attr_map.get(name)
            for name in (
                "data-message-id",
                "message-id",
                "data-id",
                "data-uid",
                "uid",
                "id",
            )
            if attr_map.get(name)
        ), None)
        if mail_id is None:
            identity_parts = (
                received_at,
                from_addr,
                subject,
                body,
            )
            normalized_identity = "\x1f".join(
                re.sub(r"\s+", " ", str(value or "")).strip()
                for value in identity_parts
            )
            digest = hashlib.sha256(normalized_identity.encode("utf-8")).hexdigest()[:24]
            mail_id = f"inline-sha256-{digest}"
        items.append({
            "mail_id": mail_id,
            "subject": subject,
            "received_at": received_at,
            "from": from_addr,
            "body": body,
            "msg_ts": msg_ts or 0.0,
        })

    items.sort(key=lambda x: float(x.get("msg_ts") or 0.0), reverse=True)
    baseline_ids = {str(value) for value in (baseline_message_ids or ())}
    for item in items:
        if str(item.get("mail_id")) in baseline_ids:
            logger.debug("[GenericAPI] inline messages 跳过基线邮件: id=%s", item.get("mail_id"))
            continue
        msg_ts = float(item.get("msg_ts") or 0.0)
        if after_ts and msg_ts and msg_ts + 2 < after_ts:
            logger.debug(
                "[GenericAPI] inline messages 跳过旧邮件: id=%s ts=%s after=%s has_subject=%s subject_len=%s",
                item.get("mail_id"), item.get("received_at"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(after_ts)),
                bool(item.get("subject")), len(str(item.get("subject") or "")),
            )
            continue
        code = _extract_yangyang_openai_code(str(item.get("subject") or ""), str(item.get("body") or ""))
        if code:
            logger.info(
                "[GenericAPI] inline messages 页面提取到 OTP 候选, mail_id=%s, ts=%s, has_subject=%s, subject_len=%s",
                item.get("mail_id"), item.get("received_at"),
                bool(item.get("subject")), len(str(item.get("subject") or "")),
            )
            return code, {
                "mail_id": item.get("mail_id"),
                "received_at": item.get("received_at"),
                "subject": item.get("subject"),
                "msg_ts": msg_ts,
            }
    return None


def pick_account() -> GenericApiEmailAccount:
    """领取一个可用通用 API 邮箱。"""
    from core.db import claim_next_generic_api_email, generic_api_email_pool_summary

    inserted, skipped = import_from_file()
    if inserted:
        logger.info(f"[GenericAPI] 已自动从 {_ACCOUNTS_FILE.name} 导入 {inserted} 个邮箱（跳过 {skipped} 个）")

    row = claim_next_generic_api_email()
    if row is None:
        summary = generic_api_email_pool_summary()
        raise GenericApiMailError(
            f"通用 API 邮箱池没有可用账号: {summary}. 请在 WebUI 邮箱池导入：邮箱----取码地址"
        )
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[GenericAPI] 选中邮箱: {account.email}（DB id={row.get('id')}）")
    return account


def import_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """从文本文件导入通用 API 邮箱，每行：email----code_url 或 email====code_url。"""
    from core.db import import_generic_api_emails
    p = Path(path) if path else _ACCOUNTS_FILE
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    if not p.exists():
        return 0, 0
    records = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----") if "----" in line else line.split("====")
        parts = [x.strip() for x in parts]
        if len(parts) < 2:
            continue
        records.append({"email": parts[0], "code_url": parts[1]})
    return import_generic_api_emails(records)


def get_account_context(email: str) -> GenericApiEmailAccount | None:
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_generic_api_email_by_email
    row = get_generic_api_email_by_email(email)
    if row is None:
        return None
    account = GenericApiEmailAccount(email=row["email"], code_url=row["code_url"])
    _CONTEXT_CACHE[email] = account
    return account


@dataclass(frozen=True)
class OtpBaseline:
    codes: frozenset[str]
    message_ids: frozenset[str]
    captured_at: float


def _otp_observation_key(observation: GenericOtpObservation) -> tuple[str, str, str]:
    """返回 message_id、msg_ts、code 的稳定字符串键。"""
    return (
        str(observation.message_id) if observation.message_id is not None else "",
        str(observation.msg_ts) if observation.msg_ts is not None else "",
        str(observation.code) if observation.code is not None else "",
    )


def _matches_otp_baseline(
    observation: GenericOtpObservation,
    baseline: OtpBaseline | None,
    after_ts: float | None,
) -> bool:
    """无新时间戳/消息 ID 证明时，判断候选是否仍是触发前基线。"""
    if baseline is None or not observation.code:
        return False
    if (
        observation.message_id
        and baseline.message_ids
        and observation.message_id not in baseline.message_ids
    ):
        return False
    if (
        observation.msg_ts is not None
        and after_ts is not None
        and observation.msg_ts + 2 >= after_ts
    ):
        return False
    if observation.message_id and baseline.message_ids:
        return True
    return observation.code in baseline.codes


def _fetch_current_observation(account: GenericApiEmailAccount) -> GenericOtpObservation:
    """只读取一次取码接口当前状态，不等待、不执行 settle。"""
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    session = requests.Session()
    if _parse_yangyang_code_url(account.code_url):
        result = _fetch_yangyang_otp(session, account.code_url, headers)
        if result:
            code, meta = result
            return GenericOtpObservation(
                code=code,
                source="yangyang",
                received_at=meta.get("received_at"),
                msg_ts=meta.get("msg_ts"),
                message_id=str(meta.get("mail_id")) if meta.get("mail_id") is not None else None,
                structured=True,
            )
        return GenericOtpObservation(None, "yangyang", None, None, None, True)

    response = session.get(account.code_url, headers=headers, timeout=20, verify=False)
    if response.status_code != 200:
        raise GenericApiMailError(f"基线接口 HTTP {response.status_code}: {(response.text or '')[:160]}")
    observation = _parse_generic_api_observation(response.text or "")
    if observation.structured:
        return observation
    return replace(
        observation,
        code=_extract_code(response.text or ""),
        source="plain_text",
    )


def capture_otp_baseline(email: str, attempts: int = 3) -> OtpBaseline:
    """在触发发信前记录取码接口状态，防止缓存验证码成为首个候选。"""
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    last_error = ""
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            observation = _fetch_current_observation(account)
            codes = frozenset({observation.code}) if observation.code else frozenset()
            message_ids = (
                frozenset({observation.message_id})
                if observation.message_id else frozenset()
            )
            baseline = OtpBaseline(codes, message_ids, time.time())
            logger.info(
                "[GenericAPI] 已抓取 OTP 基线: email=%s code_count=%s message_ids=%s captured_at=%s",
                email,
                len(baseline.codes),
                sorted(baseline.message_ids),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(baseline.captured_at)),
            )
            return baseline
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(1)

    raise GenericApiMailError(f"抓取验证码接口基线失败: {email}; {last_error}")


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    from core.db import release_generic_api_email
    release_generic_api_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
    exclude_codes=None,
    otp_baseline: OtpBaseline | None = None,
) -> str:
    """
    轮询该邮箱配置的 code_url，直到提取到 6 位验证码或超时。

    settle 机制：首次拿到验证码后不立刻返回，而是继续等 OTP_SETTLE_SECONDS 秒。
    如果期间取码地址返回了不同验证码，则替换候选并重置 settle 倒计时；
    连续 settle 秒没有变化后才返回，避免取到接口缓存中的旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise GenericApiMailError(f"通用 API 邮箱不存在或未导入: {email}")

    search_deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS
    max_age_seconds = max(
        0,
        int(getattr(_email_cfg, "OTP_MAX_MESSAGE_AGE_SECONDS", 3600) or 0),
    )
    excluded_codes = {str(value).strip() for value in (exclude_codes or ()) if str(value).strip()}
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; gpt-register/1.0)",
    }
    last_error = ""
    best_observation: GenericOtpObservation | None = None
    best_seen_at: float = 0.0
    confirm_deadline: float | None = None
    hard_confirm_deadline: float | None = None
    logger.info(
        f"[GenericAPI] 开始轮询取码地址: {email}，"
        f"最长 {max_wait or _email_cfg.OTP_MAX_WAIT}s, settle={settle}s"
    )
    is_yangyang = _parse_yangyang_code_url(account.code_url) is not None

    def update_candidate(observation: GenericOtpObservation, now_seen: float) -> None:
        """用邮件身份（而不是验证码值）判定候选是否发生更新。"""
        nonlocal best_observation, best_seen_at, confirm_deadline, hard_confirm_deadline

        if best_observation is None:
            best_observation = observation
            best_seen_at = now_seen
            confirm_deadline = now_seen + settle
            hard_confirm_deadline = now_seen + max(15, 3 * max(settle, 0))
            logger.info(
                "[GenericAPI] 首次锁定 OTP 候选: source=%s message_id=%s msg_ts=%s，等 %ss 看取码接口是否更新...",
                observation.source,
                observation.message_id,
                observation.msg_ts,
                settle,
            )
            return

        previous_key = _otp_observation_key(best_observation)
        observation_key = _otp_observation_key(observation)
        later_message = (
            observation.msg_ts is not None
            and (
                best_observation.msg_ts is None
                or observation.msg_ts > best_observation.msg_ts
            )
        )
        if observation_key != previous_key or later_message:
            best_observation = observation
            best_seen_at = now_seen
            confirm_deadline = now_seen + settle
            logger.info(
                "[GenericAPI] 发现更新 OTP 候选: source=%s message_id=%s msg_ts=%s，重置 settle 计时",
                observation.source,
                observation.message_id,
                observation.msg_ts,
            )
        else:
            logger.debug("[GenericAPI] 取码接口仍返回同一 OTP 候选")

    while True:
        now = time.time()
        if best_observation is not None:
            if hard_confirm_deadline is not None and now >= hard_confirm_deadline:
                raise GenericApiMailError(
                    f"等待通用 API 验证码失败: {email}; 候选不稳定，确认窗口内持续更新"
                )
            if confirm_deadline is not None and now >= confirm_deadline:
                logger.info(
                    "[GenericAPI] settle 完成，返回 OTP 候选: 候选锁定时间=%s",
                    time.strftime("%H:%M:%S", time.localtime(best_seen_at)),
                )
                return best_observation.code
        elif now >= search_deadline:
            break

        try:
            session = requests.Session()
            yy_after_ts = None if otp_baseline is not None else after_ts
            yy_result = _fetch_yangyang_otp(
                session,
                account.code_url,
                headers,
                after_ts=yy_after_ts,
                baseline_message_ids=(
                    otp_baseline.message_ids if otp_baseline is not None else None
                ),
            ) if is_yangyang else None
            if yy_result:
                code, yy_meta = yy_result
                if code in excluded_codes:
                    last_error = f"忽略已使用的旧 OTP: source=yangyang mail_id={yy_meta.get('mail_id')}"
                    resp = None
                    text = ""
                    code = None
                else:
                    observation = GenericOtpObservation(
                        code=code,
                        source="yangyang",
                        received_at=yy_meta.get("received_at"),
                        msg_ts=yy_meta.get("msg_ts"),
                        message_id=(
                            str(yy_meta.get("mail_id"))
                            if yy_meta.get("mail_id") is not None else None
                        ),
                        structured=True,
                    )
                    if _matches_otp_baseline(observation, otp_baseline, after_ts):
                        last_error = f"基线验证码未变化: message_id={observation.message_id}"
                        resp = None
                        text = ""
                        code = None
                if code:
                    update_candidate(observation, time.time())
                resp = None
                text = ""
            else:
                if is_yangyang:
                    last_error = "yangyang 列表中尚未出现 after_ts 之后的新验证码邮件"
                    resp = None
                    text = ""
                else:
                    resp = session.get(account.code_url, headers=headers, timeout=20, verify=False)
                    text = resp.text or ""
            if resp is None:
                pass
            elif resp.status_code == 200:
                observation = _parse_generic_api_observation(
                    text,
                    after_ts=None if otp_baseline is not None else after_ts,
                    max_age_seconds=None if otp_baseline is not None else max_age_seconds,
                )
                if observation.structured:
                    code = observation.code
                    structured_meta = {
                        "source": observation.source,
                        "received_at": observation.received_at,
                        "msg_ts": observation.msg_ts,
                        "message_id": observation.message_id,
                        "subject": observation.subject,
                    }
                    if observation.rejection_reason:
                        logger.info(
                            "[GenericAPI] OTP候选 decision=reject_candidate source=%s "
                            "message_id=%s msg_ts=%s after_ts=%s reason=%s",
                            observation.source,
                            observation.message_id,
                            observation.msg_ts,
                            after_ts,
                            observation.rejection_reason,
                        )
                        last_error = (
                            f"结构化候选被拒绝: reason={observation.rejection_reason} "
                            f"ts={observation.received_at} message_id={observation.message_id}"
                        )
                else:
                    code = _extract_code(text)
                    observation = replace(
                        observation,
                        code=code,
                        source="plain_text",
                    )
                    structured_meta = {}
                if code in excluded_codes:
                    last_error = "忽略接口返回的旧 OTP"
                    code = None
                elif code and _matches_otp_baseline(observation, otp_baseline, after_ts):
                    logger.info(
                        "[GenericAPI] OTP候选 decision=wait_for_change source=%s "
                        "message_id=%s msg_ts=%s after_ts=%s baseline_hit=True",
                        observation.source,
                        observation.message_id,
                        observation.msg_ts,
                        after_ts,
                    )
                    last_error = f"基线验证码未变化: message_id={observation.message_id}"
                    code = None
                if code:
                    update_candidate(observation, time.time())
                else:
                    last_error = "HTTP 200 但未提取到 6 位验证码"
            else:
                last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = type(exc).__name__

        now = time.time()
        if best_observation is not None:
            if hard_confirm_deadline is not None and now >= hard_confirm_deadline:
                raise GenericApiMailError(
                    f"等待通用 API 验证码失败: {email}; 候选不稳定，确认窗口内持续更新"
                )
            if confirm_deadline is not None and now >= confirm_deadline:
                logger.info(
                    "[GenericAPI] settle 完成，返回 OTP 候选: 候选锁定时间=%s",
                    time.strftime("%H:%M:%S", time.localtime(best_seen_at)),
                )
                return best_observation.code
            stage_deadline = min(
                deadline for deadline in (confirm_deadline, hard_confirm_deadline)
                if deadline is not None
            )
            logger.info(
                "[GenericAPI] 已锁定 OTP 候选，等 settle 中（剩余 settle ~%ss）...",
                max(0, int((confirm_deadline or now) - now)),
            )
        else:
            stage_deadline = search_deadline
            logger.info(
                f"[GenericAPI] 暂未从取码接口拿到验证码，"
                f"{interval}s 后重试（剩余 {max(0, int(search_deadline - now))}s）..."
            )
        remaining = max(0, stage_deadline - now)
        if remaining:
            time.sleep(min(interval, remaining))

    raise GenericApiMailError(f"等待通用 API 验证码超时: {email}; {last_error}")
