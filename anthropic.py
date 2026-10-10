"""Anthropic Messages API (/v1/messages) <-> OpenAI Chat. Dùng cho Claude Code và SDK Anthropic."""
from __future__ import annotations

import json
import uuid
from typing import Any

from .common import sse


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _image_url(block: dict[str, Any]) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    if src.get("type") == "url" and src.get("url"):
        return str(src["url"])
    return None


def anthropic_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    sys_text = _block_text(system) if system else ""
    if sys_text:
        messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages") or []:
        role = msg.get("role", "user")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            texts: list[str] = []
            calls: list[dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    calls.append({"id": b.get("id") or f"toolu_{uuid.uuid4().hex[:20]}", "type": "function",
                                  "function": {"name": b.get("name", ""),
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            m: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                m["tool_calls"] = calls
            if m["content"] is not None or calls:
                messages.append(m)
            continue
        # user: tool_result phải đứng ngay sau assistant tool_calls -> đưa lên trước
        parts: list[dict[str, Any]] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "tool_result":
                text = _block_text(b.get("content"))
                if b.get("is_error"):
                    text = f"[ERROR] {text}"
                messages.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": text})
                # ảnh trong tool_result -> gửi kèm như nội dung user
                if isinstance(b.get("content"), list):
                    for inner in b["content"]:
                        if isinstance(inner, dict) and inner.get("type") == "image":
                            url = _image_url(inner)
                            if url:
                                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "text":
                parts.append({"type": "text", "text": str(b.get("text", ""))})
            elif t == "image":
                url = _image_url(b)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "document":
                src = b.get("source") or {}
                if src.get("type") == "text" and src.get("data"):
                    parts.append({"type": "text", "text": str(src["data"])})
        if parts:
            if all(p["type"] == "text" for p in parts):
                messages.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
            else:
                messages.append({"role": "user", "content": parts})

    chat: dict[str, Any] = {"model": body.get("model"), "messages": messages, "stream": bool(body.get("stream"))}
    if body.get("max_tokens"):
        chat["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            chat[k] = body[k]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"][:4]

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "")
        if ttype.startswith("web_search"):
            chat["web_search_options"] = {}
            continue
        if tool.get("name") and (not ttype or ttype == "custom" or "input_schema" in tool):
            tools.append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}}}})
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice") or {}
        kind = tc.get("type") if isinstance(tc, dict) else None
        if kind == "any":
            chat["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif kind == "none":
            chat["tool_choice"] = "none"
        if isinstance(tc, dict) and tc.get("disable_parallel_tool_use"):
            chat["parallel_tool_calls"] = False

    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
        budget = int(thinking.get("budget_tokens") or 8000)
        chat["reasoning_effort"] = "low" if budget < 4000 else "medium" if budget < 16000 else "high"
        chat["_anthropic_thinking"] = True
    effort = (body.get("output_config") or {}).get("effort") if isinstance(body.get("output_config"), dict) else None
    if effort in ("low", "medium", "high"):
        chat["reasoning_effort"] = effort
    return chat


_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class AnthropicWriter:
    """Event nội bộ -> SSE Anthropic.

    Text/thinking stream trực tiếp. Tool call được gom lại và phát thành block hoàn chỉnh
    lúc kết thúc: provider có thể trả tham số của nhiều tool xen kẽ nhau, còn Anthropic
    yêu cầu từng block mở/đóng tuần tự.
    """

    def __init__(self, model: str, show_thinking: bool = False):
        self.id = f"msg_{uuid.uuid4().hex[:24]}"
        self.model = model
        self.show_thinking = show_thinking
        self.blocks: list[dict[str, Any]] = []
        self.open: str | None = None  # "text" | "thinking"
        self.tools: dict[int, dict[str, Any]] = {}
        self.stop = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _ev(self, name: str, data: dict[str, Any]) -> str:
        return sse({"type": name, **data}, event=name)

    def start(self) -> list[str]:
        msg = {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        return [self._ev("message_start", {"message": msg}), self._ev("ping", {})]

    def _close(self) -> list[str]:
        if self.open is None:
            return []
        out = []
        idx = len(self.blocks) - 1
        if self.open == "thinking":
            out.append(self._ev("content_block_delta", {"index": idx, "delta": {"type": "signature_delta", "signature": ""}}))
        out.append(self._ev("content_block_stop", {"index": idx}))
        self.open = None
        return out

    def _open(self, kind: str) -> list[str]:
        out = self._close()
        block = {"type": "text", "text": ""} if kind == "text" else {"type": "thinking", "thinking": "", "signature": ""}
        self.blocks.append(block)
        self.open = kind
        out.append(self._ev("content_block_start", {"index": len(self.blocks) - 1, "content_block": dict(block)}))
        return out

    def feed(self, ev: dict[str, Any]) -> list[str]:
        t = ev["type"]
        out: list[str] = []
        if t == "text":
            if self.open != "text":
                out += self._open("text")
            self.blocks[-1]["text"] += ev["text"]
            out.append(self._ev("content_block_delta", {"index": len(self.blocks) - 1,
                                                        "delta": {"type": "text_delta", "text": ev["text"]}}))
        elif t == "reasoning" and self.show_thinking:
            if self.open != "thinking":
                out += self._open("thinking")
            self.blocks[-1]["thinking"] += ev["text"]
            out.append(self._ev("content_block_delta", {"index": len(self.blocks) - 1,
                                                        "delta": {"type": "thinking_delta", "thinking": ev["text"]}}))
        elif t == "tool_start":
            self.tools[ev["index"]] = {"id": ev.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                                       "name": ev.get("name") or "", "args": ""}
        elif t == "tool_args":
            tool = self.tools.get(ev["index"])
            if tool is not None:
                tool["args"] += ev["args"]
        elif t == "usage":
            self.usage = {"input_tokens": ev.get("input", 0), "output_tokens": ev.get("output", 0)}
            if ev.get("cached"):
                self.usage["cache_read_input_tokens"] = ev["cached"]
        elif t == "finish":
            self.stop = _STOP.get(ev.get("reason") or "stop", "end_turn")
        return out

    def _stop_reason(self) -> str:
        return "tool_use" if self.tools and self.stop == "end_turn" else self.stop

    def finish(self) -> list[str]:
        out = self._close()
        for tool in self.tools.values():
            idx = len(self.blocks)
            self.blocks.append({"type": "tool_use", "id": tool["id"], "name": tool["name"], "_args": tool["args"]})
            out.append(self._ev("content_block_start", {"index": idx, "content_block": {
                "type": "tool_use", "id": tool["id"], "name": tool["name"], "input": {}}}))
            if tool["args"]:
                out.append(self._ev("content_block_delta", {"index": idx, "delta": {
                    "type": "input_json_delta", "partial_json": tool["args"]}}))
            out.append(self._ev("content_block_stop", {"index": idx}))
        out.append(self._ev("message_delta", {"delta": {"stop_reason": self._stop_reason(), "stop_sequence": None},
                                              "usage": self.usage}))
        out.append(self._ev("message_stop", {}))
        return out

    def error(self, message: str, code: str = "api_error", status: int = 502) -> list[str]:
        etype = "overloaded_error" if status in (429, 503, 529) else "api_error"
        return [self._ev("error", {"error": {"type": etype, "message": message}})]

    def result(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = [b for b in self.blocks if b["type"] != "tool_use"]
        for tool in self.tools.values():
            try:
                inp = json.loads(tool["args"]) if tool["args"].strip() else {}
            except ValueError:
                inp = {"_raw": tool["args"]}
            content.append({"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": inp})
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": content,
                "stop_reason": self._stop_reason(), "stop_sequence": None, "usage": self.usage}
