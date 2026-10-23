from __future__ import annotations

import json
from typing import Any


def sse(data: Any, event: str | None = None) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


def _part_text(content: Any) -> str:
    """Lấy phần chữ từ content (str hoặc list part kiểu OpenAI multimodal)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text") and part.get("text"):
                out.append(str(part["text"]))
        return "\n".join(out)
    return ""


def messages_text(messages: list[dict[str, Any]]) -> str:
    """Gom hội thoại OpenAI thành một prompt phẳng (cho provider đơn lượt như Gemini Web).

    Gắn nhãn vai trò để model phân biệt được lượt hệ thống/người dùng/trợ lý. Bỏ ảnh (chỉ giữ chữ).
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = _part_text(msg.get("content", ""))
        if not text:
            continue
        if role == "system":
            lines.append(f"[System]: {text}")
        elif role == "assistant":
            lines.append(f"[Assistant]: {text}")
        elif role == "tool":
            lines.append(f"[Tool result]: {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def estimate_tokens(obj: Any) -> int:
    """Ước lượng token thô (~4 ký tự/token) khi provider không trả usage."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, len(text) // 4)
