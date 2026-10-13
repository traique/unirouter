"""HTTP client dùng chung (giữ kết nối để giảm độ trễ, tiết kiệm RAM)."""
from __future__ import annotations

import httpx

from . import config

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.REQUEST_TIMEOUT, connect=config.CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60),
            follow_redirects=True,
            http2=False,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def error_message(raw: bytes | str, status: int) -> str:
    import json

    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or err.get("error_description") or err.get("code")
            if msg:
                return str(msg)[:800]
        if isinstance(err, str):
            return err[:800]
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])[:800]
    except (ValueError, AttributeError):
        pass
    if "<html" in text[:500].lower():
        return f"Upstream trả về trang HTML (HTTP {status}) - có thể bị chặn/Cloudflare"
    return (text.strip() or f"HTTP {status}")[:800]


def retry_after(headers) -> float:
    value = headers.get("retry-after") if headers else None
    if not value:
        return 0.0
    try:
        return min(float(value), 3600.0)
    except ValueError:
        return 0.0


def via_proxy() -> bool:
    """httpx tự dùng proxy từ biến môi trường; khi đó IP kết nối là của proxy."""
    import os

    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"))
