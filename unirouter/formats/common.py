from __future__ import annotations

import json
from typing import Any


def sse(data: Any, event: str | None = None) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


def estimate_tokens(obj: Any) -> int:
    """Ước lượng token thô (~4 ký tự/token) khi provider không trả usage."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)
