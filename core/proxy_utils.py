# -*- coding: utf-8 -*-
"""Proxy address normalization shared by HTTP and live-check services."""
from __future__ import annotations

from urllib.parse import quote, urlparse


def normalize_proxy_url(proxy: str | None, default_scheme: str = "socks5h") -> str:
    """Convert proxy pool formats to a curl-compatible URL."""
    value = str(proxy or "").strip()
    if not value or "://" in value:
        return value
    if "@" in value:
        host_port = value.rsplit("@", 1)[-1]
        if host_port.rsplit(":", 1)[-1].isdigit():
            return f"{default_scheme}://{value}"
    host, separator, remainder = value.partition(":")
    if not separator or not host:
        return value
    port, separator, credentials = remainder.partition(":")
    if not port.isdigit() or not (0 < int(port) <= 65535):
        return value
    if not credentials:
        return f"{default_scheme}://{host}:{port}"
    username, separator, password = credentials.partition(":")
    if not separator or not username:
        return value
    return (
        f"{default_scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}"
    )


def mask_proxy_url(proxy: str | None) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"
