"""Xuất Event nội bộ ra định dạng OpenAI Chat Completions."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .common import sse


class ChatWriter:
    def __init__(self, model: str, include_usage: bool = True):
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.model = model
        self.created = int(time.time())
        self.include_usage = include_usage
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict[str, Any]] = {}
        self.finish_reason = "stop"
        self.usage: dict[str, Any] | None = None
        self.sent_role = False

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> str:
        if not self.sent_role:
            delta = {"role": "assistant", **delta}
            self.sent_role = True
        return sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

    def start(self) -> list[str]:
        return []

    def feed(self, ev: dict[str, Any]) -> list[str]:
        t = ev["type"]
        if t == "text":
            self.text.append(ev["text"])
            return [self._chunk({"content": ev["text"]})]
        if t == "reasoning":
            self.reasoning.append(ev["text"])
            return [self._chunk({"reasoning_content": ev["text"]})]
        if t == "tool_start":
            self.tools[ev["index"]] = {"id": ev["id"], "type": "function", "function": {"name": ev["name"], "arguments": ""}}
            pos = list(self.tools).index(ev["index"])
            return [self._chunk({"tool_calls": [{"index": pos, "id": ev["id"], "type": "function",
                                                 "function": {"name": ev["name"], "arguments": ""}}]})]
        if t == "tool_args":
            tool = self.tools.get(ev["index"])
            if not tool:
                return []
            tool["function"]["arguments"] += ev["args"]
            pos = list(self.tools).index(ev["index"])
            return [self._chunk({"tool_calls": [{"index": pos, "function": {"arguments": ev["args"]}}]})]
        if t == "usage":
            self.usage = {"prompt_tokens": ev.get("input", 0), "completion_tokens": ev.get("output", 0),
                          "total_tokens": ev.get("input", 0) + ev.get("output", 0)}
            if ev.get("cached"):
                self.usage["prompt_tokens_details"] = {"cached_tokens": ev["cached"]}
        if t == "finish":
            self.finish_reason = ev.get("reason") or "stop"
        return []

    def finish(self) -> list[str]:
        out = [self._chunk({}, self.finish_reason)]
        if self.include_usage and self.usage:
            out.append(sse({"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                            "model": self.model, "choices": [], "usage": self.usage}))
        out.append("data: [DONE]\n\n")
        return out

    def error(self, message: str, code: str = "upstream_error", status: int = 502) -> list[str]:
        return [sse({"error": {"message": message, "type": "upstream_error", "code": code, "status": status}}),
                "data: [DONE]\n\n"]

    def result(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": "".join(self.text) or None}
        if self.reasoning:
            msg["reasoning_content"] = "".join(self.reasoning)
        if self.tools:
            msg["tool_calls"] = list(self.tools.values())
        out = {"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
               "choices": [{"index": 0, "message": msg, "finish_reason": self.finish_reason}]}
        if self.usage:
            out["usage"] = self.usage
        return out
