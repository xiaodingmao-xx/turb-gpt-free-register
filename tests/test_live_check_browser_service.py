# -*- coding: utf-8 -*-
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core import db


def _db_patchers(root: Path):
    return [
        patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
        patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
        patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
        patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
        patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
    ]


def test_browser_claim_and_requeue_persist_attempt_metadata():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "accounts.json").write_text(
            json.dumps([{"id": 1, "email": "user@example.com", "access_token": "old-token"}]),
            encoding="utf-8",
        )
        patchers = _db_patchers(root)
        try:
            for item in patchers:
                item.start()
            assert db.claim_account_live_check(
                1, trigger="manual", backend="browser", max_attempts=3
            )
            assert db.requeue_account_live_check(
                1,
                "TimeoutException: page load timeout",
                failure_kind="network_unavailable",
                attempt=2,
                max_attempts=3,
                next_retry_at="2026-08-18T12:00:15",
            )
            row = db.get_account(1)
        finally:
            for item in reversed(patchers):
                item.stop()

    assert row["live_check_status"] == "queued"
    assert row["live_check_backend"] == "browser"
    assert row["live_check_attempt"] == 2
    assert row["live_check_max_attempts"] == 3
    assert row["live_check_failure_kind"] == "network_unavailable"
    assert row["live_check_next_retry_at"] == "2026-08-18T12:00:15"
    assert row["access_token"] == "old-token"


def test_failed_browser_live_check_preserves_old_token_and_records_diagnostics():
    rows = [{"id": 1, "email": "user@example.com", "access_token": "old-token"}]
    result = {
        "ok": False,
        "status": "failed",
        "backend": "browser",
        "failure_kind": "profile_account_mismatch",
        "profile_id": "saved-profile",
        "profile_source": "saved",
        "proxy_used": "socks5://proxy.example:1080",
        "error": "Roxy profile 登录邮箱与目标账号不一致",
    }
    with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
        assert db.update_account_liveness(1, result)

    assert rows[0]["access_token"] == "old-token"
    assert rows[0]["live_check_backend"] == "browser"
    assert rows[0]["live_check_failure_kind"] == "profile_account_mismatch"
    assert rows[0]["live_check_profile_id"] == "saved-profile"
    assert rows[0]["live_check_profile_source"] == "saved"


def test_normalize_live_check_mode_accepts_two_modes_and_defaults_protocol():
    from core.live_check_service import normalize_live_check_mode

    assert normalize_live_check_mode(None) == "protocol"
    assert normalize_live_check_mode("protocol") == "protocol"
    assert normalize_live_check_mode("browser") == "browser"
    with pytest.raises(ValueError):
        normalize_live_check_mode("auto")


def test_browser_mode_dispatches_only_to_roxy_backend():
    from core import live_check_service as service

    slots = Mock()
    slots.acquire.return_value = True
    executor = Mock()
    executor.submit.return_value = SimpleNamespace()
    with patch.object(service, "_BROWSER_QUEUE_SLOTS", slots), patch.object(
        service, "_BROWSER_EXECUTOR", executor
    ), patch.object(service.db, "claim_account_live_check", return_value=True), patch.object(
        service, "_append_log"
    ):
        result = service.enqueue_account_live_check(
            account_id=1, email="user@example.com", mode="browser"
        )

    assert result["accepted"] is True
    assert result["mode"] == "browser"
    executor.submit.assert_called_once()
    assert executor.submit.call_args.kwargs["account_id"] == 1


def test_protocol_and_browser_modes_share_atomic_account_claim():
    from core import live_check_service as service

    slots = Mock()
    slots.acquire.return_value = True
    with patch.object(service, "_BROWSER_QUEUE_SLOTS", slots), patch.object(
        service.db, "claim_account_live_check", return_value=False
    ), patch.object(service, "_append_log"):
        result = service.enqueue_account_live_check(
            account_id=1, email="user@example.com", mode="browser"
        )

    assert result["accepted"] is False
    assert result["busy"] is True
    slots.release.assert_called_once_with()


def test_browser_retry_releases_worker_before_arming_timer():
    from core import live_check_service as service

    events = []
    with patch.object(
        service,
        "_run_browser_live_check",
        return_value={
            "ok": False,
            "status": "failed",
            "backend": "browser",
            "failure_kind": "network_unavailable",
            "retryable": True,
            "error": "page load timeout",
            "attempt": 1,
            "max_attempts": 3,
        },
    ), patch.object(
        service._BROWSER_QUEUE_SLOTS, "release", side_effect=lambda: events.append("release")
    ), patch.object(
        service, "_schedule_browser_retry", side_effect=lambda **kwargs: events.append("retry") or True
    ):
        result = service._run_browser_task_wrapper(
            account_id=1, email="user@example.com", trigger="manual"
        )

    assert result["ok"] is False
    assert events == ["release", "retry"]


def test_non_retryable_browser_failure_is_written_once_without_timer():
    from core import live_check_service as service

    result = {
        "ok": False,
        "status": "failed",
        "backend": "browser",
        "failure_kind": "profile_account_mismatch",
        "retryable": False,
        "error": "profile mismatch",
        "attempt": 1,
        "max_attempts": 3,
    }
    with patch.object(service, "_run_browser_live_check", return_value=result), patch.object(
        service.db, "update_account_liveness"
    ) as update, patch.object(service, "_schedule_browser_retry") as retry, patch.object(
        service._BROWSER_QUEUE_SLOTS, "release"
    ):
        service._run_browser_task_wrapper(
            account_id=1, email="user@example.com", trigger="manual"
        )

    retry.assert_not_called()
    update.assert_called_once_with(1, result)
