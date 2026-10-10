"""Chuyển đổi OpenAI Chat Completions <-> OpenAI Responses API."""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .common import sse

HOSTED_TOOL_TYPES = {"web_search", "web_search_preview", "image_generation", "file_search", "code_interpreter",
                     "computer_use_preview", "mcp", "local_shell"}


# ---------------------------------------------------------------- Chat -> Responses
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text")
                         and isinstance(p.get("text"), str))
    return "" if content is None else str(content)


def _blocks(role: str, content: Any) -> list[dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    out: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t in ("text", "input_text", "output_text") and isinstance(part.get("text"), str):
            out.append({"type": text_type, "text": part["text"]})
        elif t == "image_url":
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if isinstance(url, str) and url:
                block = {"type": "input_image", "image_url": url}
                if isinstance(iu, dict) and iu.get("detail"):
                    block["detail"] = iu["detail"]
                out.append(block)
        elif t == "input_image":
            out.append(dict(part))
    return out


def chat_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Trả về dict gồm instructions, input, tools, tool_choice, text (định dạng Responses)."""
    system: list[str] = []
    items: list[dict[str, Any]] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _text_of(content)
            if text:
                system.append(text)
            continue
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": str(msg.get("tool_call_id") or ""),
                          "output": _text_of(content)})
            continue
        if role not in ("user", "assistant"):
            role = "user"
        blocks = _blocks(role, content)
        if blocks:
            items.append({"type": "message", "role": role, "content": blocks})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            items.append({"type": "function_call", "call_id": str(tc.get("id") or ""),
                          "name": str(fn.get("name") or ""), "arguments": str(fn.get("arguments") or "{}")})

    out: dict[str, Any] = {"instructions": "\n\n".join(system), "input": items}

    tools: list[dict[str, Any]] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        t = tool.get("type")
        if t == "function":
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if not fn.get("name"):
                continue
            conv = {"type": "function", "name": str(fn["name"])[:128],
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}
            if fn.get("description"):
                conv["description"] = fn["description"]
            if isinstance(fn.get("strict"), bool):
                conv["strict"] = fn["strict"]
            tools.append(conv)
        elif t in HOSTED_TOOL_TYPES:
            tools.append({**tool, "type": "web_search" if t == "web_search_preview" else t})
    if body.get("web_search_options") is not None and not any(t["type"] == "web_search" for t in tools):
        opts = body["web_search_options"] if isinstance(body["web_search_options"], dict) else {}
        tools.append({"type": "web_search", **opts})
    if tools:
        out["tools"] = tools

    tc = body.get("tool_choice")
    if isinstance(tc, str) and tc in ("auto", "none", "required"):
        out["tool_choice"] = tc
    elif isinstance(tc, dict) and tc.get("type") == "function":
        name = (tc.get("function") or {}).get("name") or tc.get("name")
        if name:
            out["tool_choice"] = {"type": "function", "name": name}

    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif rf.get("type") == "json_schema" and isinstance(rf.get("json_schema"), dict):
            js = rf["json_schema"]
            fmt = {"type": "json_schema", "name": js.get("name") or "output", "schema": js.get("schema") or {}}
            if "strict" in js:
                fmt["strict"] = js["strict"]
            out["text"] = {"format": fmt}
    return out


# ---------------------------------------------------------------- Responses -> Chat
def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Request /v1/responses -> request Chat Completions (dùng cho provider không phải Codex)."""
    messages: list[dict[str, Any]] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": str(body["instructions"])})
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        raw_input = [{"type": "message", "role": "user", "content": raw_input}]
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if pending_calls:
            last = messages[-1] if messages else None
            if last and last["role"] == "assistant" and not last.get("tool_calls") and isinstance(last.get("content"), str):
                last["tool_calls"] = list(pending_calls)  # gộp text + tool_calls của cùng 1 lượt
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in raw_input or []:
        if isinstance(item, str):
            flush_calls()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        t = item.get("type") or ("message" if "role" in item else None)
        if t == "function_call":
            pending_calls.append({"id": item.get("call_id") or item.get("id") or "", "type": "function",
                                  "function": {"name": item.get("name") or "", "arguments": item.get("arguments") or "{}"}})
            continue
        if t not in ("function_call_output", "message"):
            continue  # reasoning, item_reference...: bỏ qua, không tách nhóm tool_calls
        flush_calls()
        if t == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                output = _text_of(output)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id") or "", "content": str(output or "")})
        elif t == "message":
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            content = item.get("content")
            if isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif p.get("type") == "input_image" and p.get("image_url"):
                        parts.append({"type": "image_url", "image_url": {"url": p["image_url"]}})
                if role != "user" or all(p["type"] == "text" for p in parts):
                    content = "".join(p.get("text", "") for p in parts)
                else:
                    content = parts
            messages.append({"role": role, "content": content})
    flush_calls()

    chat: dict[str, Any] = {"model": body.get("model"), "messages": messages, "stream": bool(body.get("stream"))}
    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name"):
            tools.append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}})
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, str):
            chat["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("name"):
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if body.get(k) is not None:
            chat[k] = body[k]
    effort = (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None
    if effort:
        chat["reasoning_effort"] = effort
    fmt = ((body.get("text") or {}).get("format") or {}) if isinstance(body.get("text"), dict) else {}
    if fmt.get("type") == "json_object":
        chat["response_format"] = {"type": "json_object"}
    elif fmt.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            "name": fmt.get("name") or "output", "schema": fmt.get("schema") or {}, "strict": fmt.get("strict", False)}}
    return chat


# ---------------------------------------------------------------- Events -> Responses
class ResponsesWriter:
    """Dựng output dạng Responses từ Event nội bộ (cho cả stream và non-stream)."""

    def __init__(self, model: str):
        self.id = f"resp_{uuid.uuid4().hex}"
        self.model = model
        self.created = int(time.time())
        self.seq = 0
        self.output: list[dict[str, Any]] = []
        self.msg: dict[str, Any] | None = None
        self.msg_index = -1
        self.tools: dict[int, dict[str, Any]] = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.status = "completed"

    def _ev(self, type_: str, **data: Any) -> str:
        self.seq += 1
        return sse({"type": type_, "sequence_number": self.seq, **data}, event=type_)

    def base(self, status: str) -> dict[str, Any]:
        return {"id": self.id, "object": "response", "created_at": self.created, "status": status, "model": self.model,
                "output": self.output if status != "in_progress" else [], "usage": self.usage if status != "in_progress" else None}

    def start(self) -> list[str]:
        return [self._ev("response.created", response=self.base("in_progress")),
                self._ev("response.in_progress", response=self.base("in_progress"))]

    def _close_msg(self) -> list[str]:
        if not self.msg:
            return []
        m, idx = self.msg, self.msg_index
        text = m["content"][0]["text"]
        m["status"] = "completed"
        self.msg = None
        return [self._ev("response.output_text.done", item_id=m["id"], output_index=idx, content_index=0, text=text),
                self._ev("response.content_part.done", item_id=m["id"], output_index=idx, content_index=0, part=m["content"][0]),
                self._ev("response.output_item.done", output_index=idx, item=m)]

    def feed(self, ev: dict[str, Any]) -> list[str]:
        t = ev["type"]
        out: list[str] = []
        if t == "text":
            if not self.msg:
                self.msg = {"id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
                            "status": "in_progress", "content": [{"type": "output_text", "text": "", "annotations": []}]}
                self.output.append(self.msg)
                self.msg_index = len(self.output) - 1
                out.append(self._ev("response.output_item.added", output_index=self.msg_index,
                                    item={**self.msg, "content": []}))
                out.append(self._ev("response.content_part.added", item_id=self.msg["id"], output_index=self.msg_index,
                                    content_index=0, part={"type": "output_text", "text": "", "annotations": []}))
            self.msg["content"][0]["text"] += ev["text"]
            out.append(self._ev("response.output_text.delta", item_id=self.msg["id"], output_index=self.msg_index,
                                content_index=0, delta=ev["text"]))
        elif t == "tool_start":
            out += self._close_msg()
            item = {"id": f"fc_{uuid.uuid4().hex[:24]}", "type": "function_call", "status": "in_progress",
                    "call_id": ev["id"], "name": ev["name"], "arguments": ""}
            self.output.append(item)
            self.tools[ev["index"]] = {"item": item, "pos": len(self.output) - 1}
            out.append(self._ev("response.output_item.added", output_index=len(self.output) - 1, item=item))
        elif t == "tool_args":
            slot = self.tools.get(ev["index"])
            if slot:
                slot["item"]["arguments"] += ev["args"]
                out.append(self._ev("response.function_call_arguments.delta", item_id=slot["item"]["id"],
                                    output_index=slot["pos"], delta=ev["args"]))
        elif t == "usage":
            self.usage = {"input_tokens": ev.get("input", 0), "output_tokens": ev.get("output", 0),
                          "total_tokens": ev.get("input", 0) + ev.get("output", 0),
                          "input_tokens_details": {"cached_tokens": ev.get("cached", 0)}}
        elif t == "finish":
            if ev.get("reason") == "length":
                self.status = "incomplete"
        return out

    def finish(self) -> list[str]:
        out = self._close_msg()
        for slot in self.tools.values():
            item = slot["item"]
            if item["status"] != "completed":
                item["status"] = "completed"
                out.append(self._ev("response.function_call_arguments.done", item_id=item["id"], output_index=slot["pos"],
                                    arguments=item["arguments"]))
                out.append(self._ev("response.output_item.done", output_index=slot["pos"], item=item))
        resp = self.base(self.status)
        if self.status == "incomplete":
            resp["incomplete_details"] = {"reason": "max_output_tokens"}
        out.append(self._ev("response.completed" if self.status == "completed" else "response.incomplete", response=resp))
        return out

    def error(self, message: str, code: str = "upstream_error", status: int = 502) -> list[str]:
        resp = self.base("failed")
        resp["error"] = {"code": code, "message": message}
        return [self._ev("response.failed", response=resp)]

    def result(self) -> dict[str, Any]:
        self._close_msg()
        for slot in self.tools.values():
            slot["item"]["status"] = "completed"
        resp = self.base(self.status)
        resp["output_text"] = "".join(c["text"] for o in self.output if o["type"] == "message" for c in o["content"])
        return resp


async def stream_responses(events: AsyncIterator[dict[str, Any]], model: str) -> AsyncIterator[str]:
    w = ResponsesWriter(model)
    for s in w.start():
        yield s
    async for ev in events:
        for s in w.feed(ev):
            yield s
    for s in w.finish():
        yield s
