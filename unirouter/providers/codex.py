"""Provider ChatGPT qua Codex backend (chatgpt.com/backend-api/codex/responses).

- Header giống Codex CLI, chỉ giữ các field backend chấp nhận, chế độ "responses lite"
- Reasoning effort qua hậu tố model (-low, -high...), usage_limit_reached -> khóa tài khoản đến lúc reset
- curl_cffi giả lập trình duyệt, tự retry 1 lần khi server quá tải trước khi có output
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from .. import http
from ..formats.responses import chat_to_responses
from . import codex_auth
from .base import CallContext, Event, Provider, ProviderError, Target, classify_http

ALLOWED_FIELDS = {"model", "input", "instructions", "tools", "tool_choice", "stream", "store", "reasoning",
                  "service_tier", "include", "prompt_cache_key", "text", "parallel_tool_calls"}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
TRANSIENT = ("server_is_overloaded", "service_unavailable", "overloaded", "server_error", "internal_error")
CAPACITY = ("model_at_capacity", "selected model is at capacity")
LIFECYCLE = {"response.created", "response.in_progress", "response.queued"}
OUTPUT_EVENTS = {"response.output_text.delta", "response.function_call_arguments.delta",
                 "response.reasoning_summary_text.delta", "response.output_item.added", "response.completed",
                 "response.incomplete", "response.output_item.done"}
DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def split_effort(model: str) -> tuple[str, str | None]:
    for level in EFFORTS:
        if model.endswith(f"-{level}"):
            return model[: -len(level) - 1], level
    return model, None


def is_lite(provider: dict[str, Any], model: str) -> bool:
    return model in ((provider.get("options") or {}).get("lite_models") or [])


def session_key(payload: dict[str, Any]) -> str:
    """ID ổn định theo hội thoại (dùng cho prompt cache): hash instructions + tin nhắn đầu."""
    first = payload.get("input")[0] if payload.get("input") else ""
    raw = json.dumps([payload.get("instructions", "")[:2000], first], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def normalize_tools(tools: list[Any] | None) -> list[dict[str, Any]]:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            if not fn.get("name"):
                continue
            conv = {"type": "function", "name": str(fn["name"]).strip()[:128],
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}}}
            if fn.get("description"):
                conv["description"] = fn["description"]
            if isinstance(fn.get("strict"), bool):
                conv["strict"] = fn["strict"]
            out.append(conv)
        elif tool.get("type") in ("web_search", "web_search_preview", "image_generation", "custom", "namespace",
                                  "local_shell", "file_search", "code_interpreter", "mcp"):
            out.append(tool)
    return out


def finalize_payload(provider: dict[str, Any], payload: dict[str, Any], requested_effort: str | None) -> tuple[dict[str, Any], bool]:
    """Chuẩn hóa payload Responses theo yêu cầu của Codex backend. Trả về (payload, lite)."""
    model, suffix_effort = split_effort(str(payload.get("model") or ""))
    payload["model"] = model
    lite = is_lite(provider, model)
    opts = provider.get("options") or {}

    inp = payload.get("input")
    if isinstance(inp, str):
        inp = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": inp}]}]
    inp = [i for i in (inp or []) if not (isinstance(i, dict) and i.get("type") == "item_reference")]
    for item in inp:
        if isinstance(item, dict):
            if item.get("role") == "system":
                item["role"] = "developer"
            # store=false -> backend không tra được id cũ
            if isinstance(item.get("id"), str) and item["id"][:3] in ("rs_", "fc_", "msg", "res"):
                item.pop("id", None)
    if not inp:
        inp = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "..."}]}]
    payload["input"] = inp
    payload["tools"] = normalize_tools(payload.get("tools"))
    if not payload["tools"]:
        payload.pop("tools")
    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function":
        names = {t.get("name") for t in payload.get("tools") or []}
        if tc.get("name") not in names:
            payload.pop("tool_choice")

    effort = requested_effort or suffix_effort or opts.get("default_effort") or ("medium" if lite else "low")
    reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else {}
    reasoning = dict(reasoning)
    reasoning["effort"] = reasoning.get("effort") or effort
    if reasoning["effort"] in ("max", "ultra"):
        reasoning["effort"] = "xhigh"
    if lite and reasoning["effort"] in ("none", "minimal"):
        reasoning["effort"] = "low"
    if not lite:
        reasoning.setdefault("summary", "auto")
    else:
        reasoning["context"] = "all_turns"
        reasoning.pop("summary", None)
    payload["reasoning"] = reasoning
    if reasoning["effort"] != "none":
        payload["include"] = ["reasoning.encrypted_content"]

    payload["stream"] = True
    payload["store"] = False
    if payload.get("service_tier") == "fast":
        payload["service_tier"] = "priority"
    if payload.get("service_tier") not in (None, "priority"):
        payload.pop("service_tier")
    instructions = payload.get("instructions") or ""

    if lite:
        tools = payload.pop("tools", None) or []
        prefix: list[dict[str, Any]] = [{"type": "additional_tools", "role": "developer", "tools": tools}]
        prefix.append({"type": "message", "role": "developer",
                       "content": [{"type": "input_text", "text": instructions or DEFAULT_INSTRUCTIONS}]})
        payload["input"] = prefix + payload["input"]
        payload["instructions"] = ""
        payload["tools"] = None
        payload.setdefault("tool_choice", "auto")
        payload["parallel_tool_calls"] = False
    else:
        payload["instructions"] = instructions or DEFAULT_INSTRUCTIONS
        payload.pop("parallel_tool_calls", None)

    payload.setdefault("prompt_cache_key", session_key(payload))
    for k in list(payload):
        if k not in ALLOWED_FIELDS:
            payload.pop(k)
    return payload, lite


async def is_public_url(url: str) -> bool:
    """Chống SSRF: chỉ tải ảnh từ địa chỉ IP công khai."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443,
                                                             type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return bool(infos)
    except Exception:
        return False


async def inline_remote_images(payload: dict[str, Any]) -> None:
    """Codex backend không tự tải ảnh từ URL -> tải về và nhúng base64 (tối đa 10MB/ảnh)."""
    jobs = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for block in item["content"]:
            if isinstance(block, dict) and block.get("type") == "input_image":
                url = block.get("image_url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    jobs.append(block)

    async def fetch(block: dict[str, Any]) -> None:
        try:
            url = block["image_url"]
            if not await is_public_url(url):
                return
            resp = await http.client().get(url, timeout=15, follow_redirects=False)
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if resp.status_code < 300 and ctype.startswith("image/") and len(resp.content) <= 10 * 1024 * 1024:
                block["image_url"] = f"data:{ctype};base64,{base64.b64encode(resp.content).decode()}"
        except Exception:
            pass

    if jobs:
        await asyncio.gather(*(fetch(b) for b in jobs[:8]))


def chat_to_codex(provider: dict[str, Any], model: str, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    conv = chat_to_responses(body)
    payload: dict[str, Any] = {"model": model, **conv}
    return finalize_payload(provider, payload, body.get("reasoning_effort"))


def _event_error(event: dict[str, Any]) -> tuple[str, str]:
    err = event.get("error")
    resp = event.get("response")
    if not err and isinstance(resp, dict):
        err = resp.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or err.get("type") or "").lower(), str(err.get("message") or err.get("code") or err)
    return "", str(err or event.get("message") or "ChatGPT trả về lỗi")


class CodexProvider(Provider):
    type = "codex"

    def __init__(self, store: Any):
        self.store = store

    def _headers(self, access: str, account_id: str, payload: dict[str, Any], lite: bool) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": "codex_cli_rs",
            "User-Agent": codex_auth.USER_AGENT,
            "version": codex_auth.CODEX_VERSION,
            "session_id": str(payload.get("prompt_cache_key") or "default"),
        }
        if account_id:
            h["ChatGPT-Account-ID"] = account_id
        if lite:
            h["x-openai-internal-codex-responses-lite"] = "true"
        return h

    async def _post(self, provider: dict[str, Any], connection: dict[str, Any], payload: dict[str, Any], lite: bool):
        access, account_id = await codex_auth.ensure_access_token(self.store, connection)
        for attempt in range(2):
            try:
                resp = await codex_auth.session().post(
                    provider["base_url"], headers=self._headers(access, account_id, payload, lite), json=payload,
                    stream=True, timeout=(15, 300), **codex_auth.hv(provider["base_url"]))
            except Exception as e:  # curl_cffi.CurlError
                raise ProviderError(502, f"Lỗi mạng tới ChatGPT: {e}") from e
            if resp.status_code == 401 and attempt == 0:
                await codex_auth.abort_response(resp)
                access, account_id = await codex_auth.ensure_access_token(self.store, connection, force=True)
                continue
            if resp.status_code >= 400:
                raw = await resp.acontent()
                await codex_auth.abort_response(resp)
                raise self._http_error(resp.status_code, raw, resp.headers)
            return resp
        raise ProviderError(401, "ChatGPT từ chối token", "account", 300)

    @staticmethod
    def _http_error(status: int, raw: bytes, headers: Any) -> ProviderError:
        msg = http.error_message(raw, status)
        if status == 429:
            try:
                err = json.loads(raw).get("error") or {}
            except (ValueError, AttributeError):
                err = {}
            if err.get("type") == "usage_limit_reached" or "usage_limit" in msg.lower():
                cooldown = 3600.0
                if isinstance(err.get("resets_at"), (int, float)) and err["resets_at"] > time.time():
                    cooldown = err["resets_at"] - time.time()
                elif isinstance(err.get("resets_in_seconds"), (int, float)):
                    cooldown = float(err["resets_in_seconds"])
                return ProviderError(429, f"Hết hạn mức ChatGPT: {msg}", "account", max(cooldown, 60))
        if status == 400 and "model" in msg.lower() and "not supported" in msg.lower():
            return ProviderError(400, msg, "target")
        return classify_http(status, msg, http.retry_after(headers))

    async def open(self, target: Target, connection: dict[str, Any], ctx: CallContext) -> AsyncIterator[Event]:
        provider = target.provider
        if ctx.responses_body is not None:
            payload = json.loads(json.dumps(ctx.responses_body))
            payload["model"] = target.model
            payload, lite = finalize_payload(provider, payload, None)
            raw = True
        else:
            payload, lite = chat_to_codex(provider, target.model, ctx.body)
            raw = False
        await inline_remote_images(payload)

        # Retry 1 lần khi server quá tải trước khi có output
        for attempt in range(2):
            resp = await self._post(provider, connection, payload, lite)
            it = self._iter_events(resp.aiter_lines())
            try:
                head = await self._prime(it)
            except BaseException as e:
                await codex_auth.abort_response(resp)
                if not isinstance(e, Exception):
                    raise  # CancelledError: client đã ngắt
                if not isinstance(e, ProviderError):
                    raise ProviderError(502, f"Mất kết nối ChatGPT: {e}") from e
                if attempt == 0 and e.status == 503 and e.scope == "target":
                    await asyncio.sleep(1.0)
                    continue
                raise
            return self._events(resp, it, head, raw)
        raise ProviderError(503, "ChatGPT quá tải")

    async def _iter_events(self, lines: Any) -> AsyncIterator[dict[str, Any]]:
        async for line in lines:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj

    async def _prime(self, it: Any) -> list[dict[str, Any]]:
        """Đọc tới sự kiện output đầu tiên. Lỗi xảy ra trước đó => ném ProviderError để fallback."""
        buffered: list[dict[str, Any]] = []
        async for ev in it:
            et = ev.get("type", "")
            if et in ("error", "response.failed") or ev.get("status") == "failed":
                code, msg = _event_error(ev)
                low = (code + " " + msg).lower()
                if any(c in low for c in CAPACITY):
                    raise ProviderError(503, msg, "account", 60)
                if any(c in low for c in TRANSIENT):
                    raise ProviderError(503, msg, "target")
                if "usage_limit" in low or "rate_limit" in low:
                    raise ProviderError(429, msg, "account", 300)
                if "context" in low and ("length" in low or "window" in low):
                    raise ProviderError(400, msg, "target")
                raise ProviderError(502, msg, "target")
            buffered.append(ev)
            if et not in LIFECYCLE:
                break
        return buffered

    async def _events(self, resp: Any, it: Any, head: list[dict[str, Any]], raw: bool) -> AsyncIterator[Event]:
        tool_index: dict[str, int] = {}
        args_seen: set[int] = set()
        finished = False
        try:
            async def all_events():
                for ev in head:
                    yield ev
                async for ev in it:
                    yield ev

            async for ev in all_events():
                et = ev.get("type", "")
                if raw:
                    yield {"type": "raw", "event": ev}
                    if et in ("response.completed", "response.incomplete", "response.failed"):
                        u = (ev.get("response") or {}).get("usage") or {}
                        yield {"type": "usage", "input": int(u.get("input_tokens") or 0),
                               "output": int(u.get("output_tokens") or 0),
                               "cached": int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)}
                        finished = True
                        break
                    if et == "error":
                        _, msg = _event_error(ev)
                        raise ProviderError(502, msg)
                    continue

                if et == "response.output_text.delta":
                    if ev.get("delta"):
                        yield {"type": "text", "text": ev["delta"]}
                elif et == "response.reasoning_summary_text.delta":
                    if ev.get("delta"):
                        yield {"type": "reasoning", "text": ev["delta"]}
                elif et == "response.reasoning_summary_part.done":
                    yield {"type": "reasoning", "text": "\n\n"}
                elif et == "response.output_item.added":
                    item = ev.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = len(tool_index)
                        tool_index[item.get("id") or str(idx)] = idx
                        yield {"type": "tool_start", "index": idx, "id": item.get("call_id") or item.get("id"),
                               "name": item.get("name") or ""}
                        if item.get("arguments"):
                            args_seen.add(idx)
                            yield {"type": "tool_args", "index": idx, "args": item["arguments"]}
                elif et == "response.function_call_arguments.delta":
                    idx = tool_index.get(ev.get("item_id") or "")
                    if idx is not None and ev.get("delta"):
                        args_seen.add(idx)
                        yield {"type": "tool_args", "index": idx, "args": ev["delta"]}
                elif et == "response.output_item.done":
                    item = ev.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = tool_index.get(item.get("id") or "")
                        if idx is None:
                            idx = len(tool_index)
                            tool_index[item.get("id") or str(idx)] = idx
                            yield {"type": "tool_start", "index": idx, "id": item.get("call_id"), "name": item.get("name") or ""}
                        if idx not in args_seen and item.get("arguments"):
                            args_seen.add(idx)
                            yield {"type": "tool_args", "index": idx, "args": item["arguments"]}
                elif et in ("response.completed", "response.incomplete"):
                    r = ev.get("response") or {}
                    u = r.get("usage") or {}
                    yield {"type": "usage", "input": int(u.get("input_tokens") or 0),
                           "output": int(u.get("output_tokens") or 0),
                           "cached": int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)}
                    reason = "length" if et == "response.incomplete" else ("tool_calls" if tool_index else "stop")
                    yield {"type": "finish", "reason": reason}
                    finished = True
                    break
                elif et in ("response.failed", "error"):
                    _, msg = _event_error(ev)
                    raise ProviderError(502, msg)
            if not finished:
                if raw:
                    raise ProviderError(502, "Stream ChatGPT kết thúc đột ngột")
                yield {"type": "finish", "reason": "tool_calls" if tool_index else "stop"}
        finally:
            try:
                await codex_auth.abort_response(resp)
            except Exception:
                pass

    async def images(self, target: Target, connection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        """Tạo ảnh bằng tool image_generation của Responses (dùng hạn mức ChatGPT). Thử nghiệm."""
        provider = target.provider
        opts = provider.get("options") or {}
        lite = set(opts.get("lite_models") or [])
        chat_model = opts.get("image_chat_model") or next(
            (m for m in [opts.get("auto_model"), *provider.get("models", [])] if m and m not in lite), None)
        if not chat_model:
            raise ProviderError(400, "Chưa có model chat để gọi công cụ tạo ảnh", "target")
        tool: dict[str, Any] = {"type": "image_generation", "output_format": "png"}
        size = str(body.get("size") or "auto")
        if size in ("1024x1024", "1024x1536", "1536x1024", "auto"):
            tool["size"] = size
        if body.get("quality") in ("low", "medium", "high", "auto"):
            tool["quality"] = body["quality"]
        if body.get("background") in ("transparent", "opaque", "auto"):
            tool["background"] = body["background"]
        prompt = str(body.get("prompt") or "")
        n = max(1, min(int(body.get("n") or 1), 4))
        images: list[dict[str, Any]] = []
        usage = {"input": 0, "output": 0}

        async def one() -> None:
            payload = {"model": chat_model, "instructions": "Generate exactly the image the user describes. "
                                                            "Do not ask questions.",
                       "input": [{"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": prompt}]}],
                       "tools": [tool], "tool_choice": {"type": "image_generation"}}
            payload, lite_mode = finalize_payload(provider, payload, "low")
            if lite_mode:
                raise ProviderError(400, "Model lite không hỗ trợ tạo ảnh", "target")
            ctx = CallContext({}, True, "images", payload)
            it = await self.open(Target(provider, chat_model, target.label), connection, ctx)
            found = None
            async for ev in it:
                if ev.get("type") == "usage":
                    usage["input"] += ev.get("input", 0)
                    usage["output"] += ev.get("output", 0)
                    continue
                raw = ev.get("event") or {}
                item = raw.get("item") or {}
                if raw.get("type") == "response.output_item.done" and item.get("type") == "image_generation_call":
                    if item.get("result"):
                        found = {"b64_json": item["result"], "revised_prompt": item.get("revised_prompt") or prompt}
            if not found:
                raise ProviderError(502, "ChatGPT không trả về ảnh (tài khoản có thể chưa được dùng công cụ tạo ảnh)")
            images.append(found)

        for _ in range(n):
            await one()
        return {"created": int(time.time()), "data": images,
                "usage": {"input_tokens": usage["input"], "output_tokens": usage["output"]}}

    async def list_models(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[str]:
        access, account_id = await codex_auth.ensure_access_token(self.store, connection)
        url = provider["base_url"].rsplit("/responses", 1)[0] + "/models?client_version=" + codex_auth.CODEX_VERSION
        headers = {"Authorization": f"Bearer {access}", "originator": "codex_cli_rs",
                   "User-Agent": codex_auth.USER_AGENT, "Accept": "application/json"}
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        resp = await codex_auth.session().get(url, headers=headers, timeout=30, **codex_auth.hv(url))
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, resp.content, resp.headers)
        data = resp.json()
        items = data.get("models") or data.get("data") or []
        ids = [m.get("slug") or m.get("id") for m in items if isinstance(m, dict)]
        return sorted({i for i in ids if i})
