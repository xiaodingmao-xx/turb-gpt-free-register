"""Registration-time network metadata helpers."""

from __future__ import annotations

import ipaddress
from typing import Any


def normalize_public_ip(value: Any) -> str:
    """Return a normalized IP literal, or an empty string for non-IP values."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return ""
    return str(address)


def extract_public_ip(payload: Any) -> str:
    """Extract an IP from common JSON responses without retaining arbitrary text."""
    if isinstance(payload, dict):
        for key in ("ip", "query", "address"):
            value = normalize_public_ip(payload.get(key))
            if value:
                return value
        for key in ("data", "result"):
            value = extract_public_ip(payload.get(key))
            if value:
                return value
        return ""
    return normalize_public_ip(payload)


def detect_playwright_exit_ip(page: Any) -> str:
    """Best-effort browser-context lookup through the registration page route."""
    try:
        payload = page.evaluate(
            """async () => {
                const response = await fetch('https://api.ipify.org?format=json', {cache: 'no-store'});
                return await response.json();
            }""",
            timeout=8000,
        )
    except Exception:
        return ""
    return extract_public_ip(payload)


def detect_selenium_exit_ip(driver: Any) -> str:
    """Best-effort browser-context lookup for Selenium-compatible drivers."""
    try:
        payload = driver.execute_async_script(
            """const done = arguments[arguments.length - 1];
            fetch('https://api.ipify.org?format=json', {cache: 'no-store'})
              .then(response => response.json())
              .then(done)
              .catch(() => done(null));"""
        )
    except Exception:
        return ""
    return extract_public_ip(payload)
