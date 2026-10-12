"""Provider tương thích OpenAI (NVIDIA NIM, Groq, OpenRouter, TokenRouter, Cerebras, Gemini...)."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .. import http
from .base import CallContext, Event, Provider, ProviderError, Target, classify_http

# Trường chỉ dùng nội bộ hoặc chỉ Codex hiểu -> bỏ khi gửi provider khác
_DROP_FIELDS = {"store", "metadata", "service_tier", "prompt_cache_key", "safety_identifier", "web_search_options"}


def build_body(target: Target, ctx: CallContext) -> dict[str, Any]:
    body = {k: v for k, v in ctx.body.items() if k not in _DROP_FIELDS and not k.startswith("_")}
    body["model"] = target.model
    body["stream"] = True
    opts = target.provider.get("options") or {}
    if opts.get("stream_usage", True):
        body["stream_options"] = {"include_usage": True}
    else:
        body.pop("stream_options", None)
    # Một số provider không nhận tool_choice khi không có tools
    if not body.get("tools"):
        body.pop("tools", None)
        body.pop("tool_choice", None)
        body.pop("parallel_tool_calls", None)
    return body


# Header tự thêm theo domain (người dùng không cần biết/nhập)
AUTO_HEADERS = {
    "openrouter.ai": {"HTTP-Referer": "https://github.com/traique/unirouter", "X-Title": "UniRouter"},
}


def auto_headers(base_url: str) -> dict[str, str]:
    for domain, hdrs in AUTO_HEADERS.items():
        if domain in base_url:
            return dict(hdrs)
    return {}


def headers_for(provider: dict[str, Any], secret: dict[str, Any]) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if secret.get("api_key"):
        h["Authorization"] = f"Bearer {secret['api_key']}"
    h.update(auto_headers(provider.get("base_url") or ""))
    for k, v in (provider.get("headers") or {}).items():
        h[str(k)] = str(v)
    return h


class ChunkParser:
    """Chuyển chunk OpenAI Chat (stream) thành Event nội bộ."""

    def __init__(self) -> None:
        self.tool_seen: dict[int, bool] = {}
        self.next_index = 0
        self.index_map: dict[Any, int] = {}
        self.finish: str | None = None
        self.usage: dict[str, int] | None = None

    def _usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details") or {}
            self.usage = {
                "input": int(usage.get("prompt_tokens") or 0),
                "output": int(usage.get("completion_tokens") or 0),
                "cached": int((details or {}).get("cached_tokens") or 0),
            }

    def feed(self, data: dict[str, Any]) -> list[Event]:
        if "error" in data and not data.get("choices"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = 0
            if isinstance(err, dict):
                try:
                    code = int(err.get("code") or err.get("status") or 0)
                except (TypeError, ValueError):
                    code = 0
            raise classify_http(code or 502, str(msg or "Upstream stream error"))
        out: list[Event] = []
        self._usage(data.get("usage"))
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.append({"type": "reasoning", "text": reasoning})
            content = delta.get("content")
            if isinstance(content, str) and content:
                out.append({"type": "text", "text": content})
            for tc in delta.get("tool_calls") or []:
                raw_idx = tc.get("index")
                key = raw_idx if raw_idx is not None else tc.get("id") or self.next_index
                if key not in self.index_map:
                    self.index_map[key] = self.next_index
                    self.next_index += 1
                idx = self.index_map[key]
                fn = tc.get("function") or {}
                if idx not in self.tool_seen:
                    self.tool_seen[idx] = True
                    out.append({"type": "tool_start", "index": idx, "id": tc.get("id") or f"call_{idx}",
                                "name": fn.get("name") or ""})
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    out.append({"type": "tool_args", "index": idx, "args": args})
                elif isinstance(args, dict):
                    out.append({"type": "tool_args", "index": idx, "args": json.dumps(args)})
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        return out

    def tail(self) -> list[Event]:
        out: list[Event] = []
        if self.usage:
            out.append({"type": "usage", **self.usage})
        reason = self.finish or "stop"
        if self.tool_seen and reason == "stop":
            reason = "tool_calls"
        out.append({"type": "finish", "reason": reason})
        return out


async def iter_sse(resp: httpx.Response) -> AsyncIterator[str]:
    """Trả về chuỗi data của từng sự kiện SSE."""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


class OpenAICompatProvider(Provider):
    type = "openai"

    def __init__(self, on_option_learned=None):
        self.on_option_learned = on_option_learned  # lưu lại tùy chọn tự học được vào DB

    async def open(self, target: Target, connection: dict[str, Any], ctx: CallContext) -> AsyncIterator[Event]:
        secret = connection["_secret"]
        provider = target.provider
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        body = build_body(target, ctx)
        client = http.client()
        for attempt in range(2):
            req = client.build_request("POST", url, json=body, headers=headers_for(provider, secret))
            try:
                resp = await client.send(req, stream=True)
            except httpx.TimeoutException as e:
                raise ProviderError(504, f"Timeout khi kết nối {provider['name']}: {e}") from e
            except httpx.HTTPError as e:
                raise ProviderError(502, f"Lỗi mạng tới {provider['name']}: {e}") from e

            if resp.status_code < 400:
                return self._events(resp)
            raw = await resp.aread()
            await resp.aclose()
            msg = http.error_message(raw, resp.status_code)
            # Tự nhận biết provider không hỗ trợ stream_options -> bỏ đi, ghi nhớ, thử lại 1 lần
            if (attempt == 0 and "stream_options" in body and resp.status_code in (400, 422)
                    and any(k in msg.lower() for k in ("stream_options", "include_usage", "unrecognized", "unknown field",
                                                       "extra inputs", "not permitted", "additional properties"))):
                provider.setdefault("options", {})["stream_usage"] = False
                if self.on_option_learned:
                    self.on_option_learned(provider["id"], "stream_usage", False)
                body.pop("stream_options", None)
                continue
            raise classify_http(resp.status_code, msg, http.retry_after(resp.headers))
        raise ProviderError(502, "Provider từ chối request")

    async def _events(self, resp: httpx.Response) -> AsyncIterator[Event]:
        parser = ChunkParser()
        try:
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                data = json.loads(await resp.aread())
                for ev in parser.feed(data):
                    yield ev
            else:
                async for data in iter_sse(resp):
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        for ev in parser.feed(obj):
                            yield ev
            for ev in parser.tail():
                yield ev
        except httpx.HTTPError as e:
            raise ProviderError(502, f"Mất kết nối stream: {e}") from e
        finally:
            await resp.aclose()

    async def list_models(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[str]:
        url = provider["base_url"].rstrip("/") + "/models"
        h = headers_for(provider, connection["_secret"])
        h["Accept"] = "application/json"
        resp = await http.client().get(url, headers=h, timeout=30)
        if resp.status_code >= 400:
            raise classify_http(resp.status_code, http.error_message(resp.content, resp.status_code))
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        ids = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else it
            if isinstance(mid, str):
                ids.append(mid.removeprefix("models/"))
        return sorted(set(ids))
