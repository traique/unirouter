"""Endpoint /v1: Chat Completions, Responses, Anthropic Messages, Embeddings, Images, Models."""
from __future__ import annotations

import asyncio
import re
import copy
import hmac
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import config, images, rtk
from .formats.anthropic import AnthropicWriter, anthropic_to_chat
from .formats.chat import ChatWriter
from .formats.common import estimate_tokens, sse
from .formats.responses import ResponsesWriter, responses_to_chat
from .providers.base import CallContext
from .router import GatewayError, Stream

EARLY_WAIT = 8.0      # chờ tối đa để trả mã lỗi HTTP đúng trước khi mở stream
KEEPALIVE = 15.0      # gửi ": keepalive" khi model còn đang nghĩ (tránh proxy cắt kết nối)

router = APIRouter()


def S(request: Request):
    return request.app.state.app


# ------------------------------------------------------------------ auth
def extract_key(request: Request) -> str:
    h = request.headers
    key = h.get("x-api-key") or h.get("api-key") or ""
    if not key:
        auth = h.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        key = token if scheme.lower() == "bearer" else auth
    return key.strip()


def authenticate(request: Request) -> dict[str, Any]:
    raw = extract_key(request)
    st = S(request)
    if raw and config.MASTER_API_KEY and hmac.compare_digest(raw.encode(), config.MASTER_API_KEY.encode()):
        return {"id": "master", "name": "master", "allowed_models": None, "rpm": 0}
    key = st.store.lookup_key(raw) if raw else None
    if not key:
        hint = "" if (config.MASTER_API_KEY or st.store.keys) else " Chưa có API key nào: tạo trong /admin."
        raise HTTPException(401, f"API key không hợp lệ.{hint}")
    ok, wait = st.limiter.allow(key["id"], int(key.get("rpm") or 0))
    if not ok:
        raise HTTPException(429, f"Vượt giới hạn {key['rpm']} request/phút. Thử lại sau {int(wait) + 1}s",
                            headers={"Retry-After": str(int(wait) + 1)})
    return key


def error_body(fmt: str, status: int, message: str) -> dict[str, Any]:
    if fmt == "anthropic":
        etype = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                 404: "not_found_error", 429: "rate_limit_error", 413: "request_too_large"}.get(status, "api_error")
        if status in (502, 503):
            etype = "overloaded_error" if status == 503 else "api_error"
        return {"type": "error", "error": {"type": etype, "message": message}}
    etype = "invalid_request_error" if status < 500 else "upstream_error"
    return {"error": {"message": message, "type": etype, "code": status}}


async def read_json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, "Body không phải JSON hợp lệ") from e
    if not isinstance(data, dict):
        raise HTTPException(400, "Body phải là object JSON")
    return data


# ------------------------------------------------------------------ core
def make_writer(fmt: str, model: str, chat: dict[str, Any]):
    if fmt == "anthropic":
        return AnthropicWriter(model, show_thinking=bool(chat.get("_anthropic_thinking")))
    if fmt == "responses":
        return ResponsesWriter(model)
    include_usage = bool((chat.get("stream_options") or {}).get("include_usage", False))
    return ChatWriter(model, include_usage)


def raw_frame(ev: dict[str, Any]) -> str:
    return sse(ev, event=ev.get("type"))


def raw_result(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Dựng response object từ sự kiện Codex gốc (cho /v1/responses không stream)."""
    final: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") == "response.output_item.done" and isinstance(ev.get("item"), dict):
            items.append(ev["item"])
        if ev.get("type") in ("response.completed", "response.incomplete") and isinstance(ev.get("response"), dict):
            final = ev["response"]
    if not final.get("output"):
        final["output"] = items
    final.setdefault("object", "response")
    final["output_text"] = "".join(c.get("text", "") for o in final.get("output") or [] if o.get("type") == "message"
                                   for c in o.get("content") or [] if c.get("type") == "output_text")
    return final


async def handle(request: Request, fmt: str, chat: dict[str, Any], requested: str, stream: bool,
                 responses_body: dict[str, Any] | None = None) -> Any:
    st = S(request)
    key = authenticate(request)
    if not st.router.allowed(key, requested):
        return JSONResponse(error_body(fmt, 403, f"API key không được phép dùng model '{requested}'"), 403)

    rtk_saved = 0
    if st.store.setting("rtk_enabled"):
        rtk_saved += rtk.compress_chat(chat)
        if responses_body is not None:
            rtk_saved += rtk.compress_responses(responses_body)

    ctx = CallContext(chat, stream, fmt, responses_body)
    t0 = time.monotonic()
    log = {"key_name": key["name"], "key_id": key["id"], "endpoint": fmt, "requested_model": requested, "rtk_saved": rtk_saved}
    task = asyncio.create_task(st.router.open(ctx, requested, allow_default=not key.get("allowed_models")))
    done, _ = await asyncio.wait({task}, timeout=EARLY_WAIT if stream else None)

    if task in done and task.exception() is not None:
        return failure_response(st, fmt, task.exception(), log, t0)

    if not stream:
        try:
            s: Stream = await task
        except Exception as e:  # noqa: BLE001
            return failure_response(st, fmt, e, log, t0)
        return await collect(st, fmt, s, chat, log, t0)

    return StreamingResponse(stream_body(st, fmt, task, chat, log, t0), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def failure_response(st, fmt: str, exc: BaseException, log: dict[str, Any], t0: float) -> JSONResponse:
    if isinstance(exc, GatewayError):
        status, msg, attempts = exc.status, exc.message, len(exc.attempts)
    elif isinstance(exc, HTTPException):
        status, msg, attempts = exc.status_code, str(exc.detail), 0
    else:
        status, msg, attempts = 500, f"Lỗi nội bộ: {exc}", 0
    st.usage.add(**log, status=status, latency_ms=int((time.monotonic() - t0) * 1000), attempts=attempts, error=msg)
    headers = {"Retry-After": "30"} if status == 429 else None
    return JSONResponse(error_body(fmt, status, msg), status_code=status, headers=headers)


def _log_done(st, s: Stream, log: dict[str, Any], t0: float, status: int, writer_usage: dict[str, int],
              chat: dict[str, Any], out_chars: int, error: str | None = None) -> None:
    inp = writer_usage.get("input") or estimate_tokens(chat.get("messages") or "")
    outp = writer_usage.get("output") or max(0, out_chars // 4)
    st.usage.add(**log, provider=s.target.provider["id"], model=s.target.model, connection=s.connection["label"],
                 status=status, latency_ms=int((time.monotonic() - t0) * 1000), ttfb_ms=s.ttfb_ms,
                 input_tokens=inp, output_tokens=outp, attempts=len(s.attempts) + 1, error=error)


async def collect(st, fmt: str, s: Stream, chat: dict[str, Any], log: dict[str, Any], t0: float) -> JSONResponse:
    is_raw = fmt == "responses" and s.target.provider["type"] == "codex" and s.first.get("type") == "raw"
    writer = make_writer(fmt, chat.get("model") or s.target.label, chat)
    raw_events: list[dict[str, Any]] = []
    usage: dict[str, int] = {}
    chars = 0
    try:
        async for ev in s.events():
            if ev["type"] == "raw":
                raw_events.append(ev["event"])
            elif ev["type"] == "usage":
                usage = ev
                if not is_raw:
                    writer.feed(ev)
            else:
                if ev["type"] in ("text", "tool_args", "reasoning"):
                    chars += len(ev.get("text") or ev.get("args") or "")
                writer.feed(ev)
    except Exception as e:  # noqa: BLE001
        st.router.done(s, False, str(e))
        _log_done(st, s, log, t0, 502, usage, chat, chars, str(e))
        msg = getattr(e, "message", None) or str(e)
        return JSONResponse(error_body(fmt, 502, f"Upstream lỗi giữa chừng: {msg}"), 502)
    finally:
        await s.aclose()
    st.router.done(s, True)
    _log_done(st, s, log, t0, 200, usage, chat, chars)
    body = raw_result(raw_events) if is_raw else writer.result()
    if fmt != "anthropic" and isinstance(body, dict):
        body.setdefault("model", s.target.label)
    return JSONResponse(body, headers={"X-UniRouter-Target": s.target.label})


async def stream_body(st, fmt: str, task: asyncio.Task, chat: dict[str, Any], log: dict[str, Any],
                      t0: float) -> AsyncIterator[str]:
    model = chat.get("model") or ""
    writer = make_writer(fmt, model, chat)
    s: Stream | None = None
    usage: dict[str, int] = {}
    chars = 0
    ok = False
    err_msg: str | None = None
    try:
        # Còn đang chờ provider (đang fallback hoặc model đang suy nghĩ) -> giữ kết nối
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=KEEPALIVE)
            if not done:
                yield ": keepalive\n\n"
        exc = task.exception()
        if exc is not None:
            status = exc.status if isinstance(exc, GatewayError) else 500
            message = exc.message if isinstance(exc, GatewayError) else str(exc)
            st.usage.add(**log, status=status, latency_ms=int((time.monotonic() - t0) * 1000), error=message,
                         attempts=len(getattr(exc, "attempts", []) or []))
            frames = writer.start() + writer.error(message, status=status)
            for frame in frames:
                yield frame
            return
        s = task.result()
        is_raw = s.first.get("type") == "raw"
        if not is_raw:
            for frame in writer.start():
                yield frame
        async for ev in s.events():
            t = ev["type"]
            if t == "raw":
                yield raw_frame(ev["event"])
                continue
            if t == "usage":
                usage = ev
            elif t in ("text", "tool_args", "reasoning"):
                chars += len(ev.get("text") or ev.get("args") or "")
            if not is_raw:
                for frame in writer.feed(ev):
                    yield frame
        if not is_raw:
            for frame in writer.finish():
                yield frame
        ok = True
    except asyncio.CancelledError:
        err_msg = "client ngắt kết nối"
        raise
    except Exception as e:  # noqa: BLE001 - lỗi giữa stream: báo trong stream
        err_msg = getattr(e, "message", None) or str(e)
        for frame in writer.error(f"Upstream lỗi giữa chừng: {err_msg}"):
            yield frame
    finally:
        if not task.done():
            task.cancel()
        if s is not None:
            await s.aclose()
            st.router.done(s, ok or err_msg == "client ngắt kết nối", err_msg)
            _log_done(st, s, log, t0, 200 if ok else 499 if err_msg == "client ngắt kết nối" else 502,
                      usage, chat, chars, None if ok else err_msg)


# ------------------------------------------------------------------ routes
@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    body = await read_json(request)
    if not isinstance(body.get("messages"), list):
        raise HTTPException(400, "messages phải là mảng")
    requested = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    return await handle(request, "chat", body, requested, stream)


@router.post("/v1/responses")
@router.post("/responses")
async def responses(request: Request):
    body = await read_json(request)
    requested = str(body.get("model") or "")
    stream = bool(body.get("stream", False))
    chat = responses_to_chat(copy.deepcopy(body))
    return await handle(request, "responses", chat, requested, stream, responses_body=body)


@router.post("/v1/messages")
@router.post("/messages")
async def messages(request: Request):
    body = await read_json(request)
    requested = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    chat = anthropic_to_chat(body)
    return await handle(request, "anthropic", chat, requested, stream)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    authenticate(request)
    body = await read_json(request)
    return {"input_tokens": estimate_tokens({"s": body.get("system"), "m": body.get("messages"), "t": body.get("tools")})}


# ------------------------------------------------------------------ embeddings / images
def public_base(request: Request) -> str:
    return config.PUBLIC_URL or str(request.base_url).rstrip("/")


async def unary(request: Request, kind: str, body: dict[str, Any], key: dict[str, Any] | None = None) -> JSONResponse:
    st = S(request)
    key = key or authenticate(request)
    requested = str(body.get("model") or "")
    if not st.router.allowed(key, requested or "auto"):
        return JSONResponse(error_body("chat", 403, f"API key không được phép dùng model '{requested or 'auto'}'"), 403)
    log = {"key_name": key["name"], "key_id": key["id"], "endpoint": kind, "requested_model": requested or "auto", "rtk_saved": 0}
    t0 = time.monotonic()
    try:
        data, target, conn, attempts, _ = await st.router.call(kind, requested, body,
                                                               allow_default=not key.get("allowed_models"))
    except Exception as e:  # noqa: BLE001
        return failure_response(st, "chat", e, log, t0)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    outp = int(usage.get("output_tokens") or 0)
    if kind == "embeddings":
        if not inp:
            inp = estimate_tokens(body.get("input"))
        data["model"] = target.label
        data.setdefault("object", "list")
    else:
        finish_images(request, data, body.get("response_format"))
        outp = outp or len(data.get("data") or [])
    ms = int((time.monotonic() - t0) * 1000)
    st.usage.add(**log, provider=target.provider["id"], model=target.model, connection=conn["label"], status=200,
                 latency_ms=ms, ttfb_ms=ms, input_tokens=inp, output_tokens=outp, attempts=len(attempts) + 1)
    return JSONResponse(data, headers={"X-UniRouter-Target": target.label})


def finish_images(request: Request, data: dict[str, Any], fmt: Any) -> None:
    """Ảnh base64 -> lưu tạm và thêm url, vì nhiều bot chỉ đọc data[0].url."""
    base = public_base(request)
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json") and not item.get("url") and fmt != "b64_json":
            name = images.save_b64(item["b64_json"])
            if name:
                item["url"] = f"{base}/v1/images/files/{name}"
                if fmt == "url":
                    item.pop("b64_json", None)


@router.post("/v1/embeddings")
@router.post("/embeddings")
async def embeddings(request: Request):
    key = authenticate(request)
    body = await read_json(request)
    if body.get("input") in (None, "", []):
        raise HTTPException(400, "Thiếu input")
    return await unary(request, "embeddings", body, key)


@router.post("/v1/images/generations")
@router.post("/images/generations")
async def images_generations(request: Request):
    key = authenticate(request)
    body = await read_json(request)
    validate_image_body(body)
    return await unary(request, "images", body, key)


def validate_image_body(body: dict[str, Any]) -> None:
    if not isinstance(body.get("prompt"), str) or not body["prompt"].strip():
        raise HTTPException(400, "Thiếu prompt")
    n = body.get("n", 1)
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10):
        raise HTTPException(400, "n phải là số nguyên từ 1 đến 10")
    size = body.get("size")
    if size is not None and (not isinstance(size, str) or not re.fullmatch(r"auto|\d{2,5}x\d{2,5}", size)):
        raise HTTPException(400, "size phải có dạng 1024x1024 hoặc auto")


@router.get("/v1/images/files/{name}")
async def image_file(name: str):
    path = images.path_of(name)
    if not path:
        raise HTTPException(404, "Ảnh không tồn tại hoặc đã hết hạn")
    return FileResponse(path, media_type=images.media_type(name), headers={"Cache-Control": "public, max-age=86400"})


@router.get("/v1/models")
@router.get("/models")
async def models(request: Request):
    key = authenticate(request)
    st = S(request)
    created = int(time.time())
    data = []
    if "auto" not in st.store.combos and st.router.allowed(key, "auto"):
        data.append({"id": "auto", "object": "model", "created": created, "owned_by": "unirouter"})
    for c in st.store.combos.values():
        if c["enabled"] and st.router.allowed(key, c["name"]):
            data.append({"id": c["name"], "object": "model", "created": created, "owned_by": "combo"})
    for p in st.store.providers.values():
        if not p["enabled"] or not any(c["enabled"] for c in st.store.provider_connections(p["id"])):
            continue
        extra = [m for m in (st.router.kind_model(p, "embeddings"), st.router.kind_model(p, "images"))
                 if m and m not in p["models"]]
        for m in [*p["models"], *extra]:
            mid = f"{p['alias']}/{m}"
            if st.router.allowed(key, mid):
                data.append({"id": mid, "object": "model", "created": created, "owned_by": p["id"]})
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
async def model_info(model_id: str, request: Request):
    authenticate(request)
    return {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "unirouter"}
