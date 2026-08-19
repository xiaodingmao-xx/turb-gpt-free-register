# -*- coding: utf-8 -*-
"""已有账号 Roxy 后台任务共用的 Profile 解析与失效恢复。"""
from __future__ import annotations

import json


def profile_id_for_account(account: dict | None) -> str:
    account = account or {}
    raw = account.get("extra_json")
    if isinstance(raw, dict):
        extra = raw
    else:
        try:
            extra = json.loads(raw) if str(raw or "").strip() else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    roxy = extra.get("roxybrowser") if isinstance(extra, dict) else {}
    return str((roxy or {}).get("profile_id") or "").strip() if isinstance(roxy, dict) else ""


def is_stale_profile_open_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "http 404", "http 502", "http 503", "profile not found",
        "dirid", "窗口/数据不存在", "数据不存在",
    ))


def open_account_profile_with_recovery(client, profile_id: str, *, progress_callback=None):
    progress = progress_callback or (lambda _message, **_kwargs: None)
    if not profile_id:
        opened = client.open_profile("", allow_existing_profile=True)
        progress(f"已创建新 Roxy 环境 profile_id={opened.profile_id}")
        return opened
    try:
        return client.open_profile(profile_id, allow_existing_profile=True)
    except Exception as original_exc:
        if not is_stale_profile_open_error(original_exc):
            raise
        progress(f"原 Roxy 环境不可用，创建临时环境 profile_id={profile_id}", level="WARNING")
        fresh_profile_id = client.create_profile()
        opened = client.open_profile(fresh_profile_id, allow_existing_profile=True)
        opened.created_by_run = True
        progress(f"已创建新 Roxy 环境 profile_id={fresh_profile_id}")
        return opened
