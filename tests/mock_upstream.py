"""Upstream giả lập: 1 provider tương thích OpenAI + Codex backend + OAuth token endpoint."""
from __future__ import annotations

import asyncio
import base64
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
calls: dict[str, int] = {}
last_body: dict[str, dict] = {}


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def access_token(exp_in: int = 3600) -> str:
    return jwt({"exp": int(time.time()) + exp_in,
                "https://api.openai.com/auth": {"chatgpt_account_id": "acc-123", "chatgpt_plan_type": "plus"},
                "https://api.openai.com/profile": {"email": "test@example.com"}})


def chunk(delta: dict, finish=None, usage=None) -> str:
    d = {"id": "x", "object": "chat.completion.chunk", "model": "m",
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        d["usage"] = usage
    return f"data: {json.dumps(d)}\n\n"


@app.post("/oai/v1/chat/completions")
async def oai_chat(request: Request):
    body = await request.json()
    model = body["model"]
    calls[model] = calls.get(model, 0) + 1
    last_body[model] = body
    auth = request.headers.get("authorization", "")
    if auth == "Bearer bad-key":
        return JSONResponse({"error": {"message": "Invalid API key"}}, 401)
    if model == "fail500":
        return JSONResponse({"error": {"message": "internal boom"}}, 500)
    if model == "rate429":
        return JSONResponse({"error": {"message": "rate limited"}}, 429, headers={"retry-after": "7"})
    if model == "badreq":
        return JSONResponse({"error": {"message": "temperature must be <= 2"}}, 400)
    if model == "nomodel":
        return JSONResponse({"error": {"message": "The model `nomodel` does not exist"}}, 404)
    if model == "nostreamopt" and "stream_options" in body:
        return JSONResponse({"error": {"message": "Unrecognized request argument supplied: stream_options"}}, 400)
    if model == "nostream":
        return {"id": "x", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "json-reply"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    async def gen():
        if model == "slow":
            await asyncio.sleep(1.5)
        if model == "tool":
            yield chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                              "function": {"name": "get_weather", "arguments": ""}}]})
            yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{\"city\":"}}]})
            yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"Hanoi\"}"}}]})
            yield chunk({}, "tool_calls")
        elif model == "midfail":
            yield chunk({"content": "partial"})
            raise RuntimeError("connection dropped")
        elif model == "streamerr":
            yield f"data: {json.dumps({'error': {'message': 'overloaded', 'code': 503}})}\n\n"
            return
        else:
            yield chunk({"role": "assistant", "reasoning_content": "thinking..."})
            yield chunk({"content": "Hello"})
            yield chunk({"content": f" from {model}"})
            yield chunk({}, "stop")
        if body.get("stream_options", {}).get("include_usage"):
            yield f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 11, 'completion_tokens': 5}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


@app.post("/oai/v1/embeddings")
async def oai_embeddings(request: Request):
    body = await request.json()
    model = body["model"]
    calls["emb:" + model] = calls.get("emb:" + model, 0) + 1
    last_body["emb:" + model] = body
    if model == "emb-fail":
        return JSONResponse({"error": {"message": "embedding backend down"}}, 503)
    if model == "emb-needtype" and "input_type" not in body:
        return JSONResponse({"error": {"message": "'input_type' parameter is required for asymmetric models"}}, 400)
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {"object": "list", "model": model,
            "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i, _ in enumerate(inputs)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7}}


@app.post("/oai/v1/images/generations")
async def oai_images(request: Request):
    body = await request.json()
    model = body["model"]
    calls["img:" + model] = calls.get("img:" + model, 0) + 1
    last_body["img:" + model] = body
    if model == "img-fail":
        return JSONResponse({"error": {"message": "image backend down"}}, 500)
    if model == "img-url":
        return {"created": 1, "data": [{"url": "https://cdn.example.com/a.png"}]}
    if "response_format" in body and model == "img-noformat":
        return JSONResponse({"error": {"message": "Unknown parameter: response_format"}}, 400)
    return {"created": 1, "data": [{"b64_json": PNG_B64}]}


@app.get("/oai/v1/models")
async def oai_models():
    return {"data": [{"id": "m1"}, {"id": "m2"}]}


@app.get("/catalog/models")
async def catalog_models():
    return [{"id": "zeta-paid"}, {"id": "ok"}, {"id": "alpha-free"}]


@app.get("/catalog/rich")
async def catalog_rich():
    """Kiểu OpenRouter: giá trong pricing, model miễn phí giá 0 hoặc đuôi :free; có model giọng nói."""
    return {"data": [{"id": "or/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                     {"id": "or/zero", "pricing": {"prompt": "0", "completion": "0"}},
                     {"id": "or/gemma:free", "pricing": {"prompt": "0", "completion": "0"}},
                     {"id": "or/whisper-free", "pricing": {"prompt": "0", "completion": "0"}},
                     {"id": "or/lyria-audio", "pricing": {"prompt": "0", "completion": "0"},
                      "architecture": {"output_modalities": ["audio"]}},
                     {"id": "tg/input-output", "pricing": {"input": "0.5", "output": "1.5"}},
                     {"id": "ok"}]}


@app.get("/catalog/plain")
async def catalog_plain():
    """Provider cả gói miễn phí (kiểu Groq): không đánh dấu gì, có model không phải chat."""
    return {"data": [{"id": "p2"}, {"id": "p1"}, {"id": "tts-1"}, {"id": "ok"}]}


def ev(d: dict) -> str:
    return f"event: {d['type']}\ndata: {json.dumps(d)}\n\n"


@app.post("/codex/responses")
async def codex(request: Request):
    body = await request.json()
    model = body["model"]
    calls["codex:" + model] = calls.get("codex:" + model, 0) + 1
    last_body["codex:" + model] = {"body": body, "headers": dict(request.headers)}
    if not request.headers.get("authorization", "").startswith("Bearer ey"):
        return JSONResponse({"detail": "Unauthorized"}, 401)
    if model == "gpt-limit":
        return JSONResponse({"error": {"type": "usage_limit_reached", "message": "limit",
                                       "resets_in_seconds": 1200}}, 429)
    if model == "gpt-gone":
        return JSONResponse({"error": {"type": "invalid_request_error", "code": "unsupported_model", "message":
                                       f"The '{model}' model is not supported when using Codex with a ChatGPT account."}},
                            400)
    if model == "gpt-badparam":  # lỗi tham số request: có chữ "model" + "not supported" nhưng KHÔNG phải model hỏng
        return JSONResponse({"error": {"type": "invalid_request_error", "code": "unsupported_parameter", "param": "text.verbosity",
                                       "message": "Unsupported parameter: 'text.verbosity' is not supported with this model."}}, 400)

    async def gen():
        yield ev({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
        if model == "gpt-overload" and calls["codex:" + model] == 1:
            yield ev({"type": "response.failed", "response": {"status": "failed", "error": {
                "code": "server_is_overloaded", "message": "Our servers are overloaded"}}})
            return
        if model == "gpt-slow":
            await asyncio.sleep(2.5)
            yield ev({"type": "response.output_text.delta", "item_id": "m", "delta": "late"})
            calls["gpt-slow-finished"] = calls.get("gpt-slow-finished", 0) + 1
            yield ev({"type": "response.completed", "response": {"usage": {}}})
            return
        if isinstance(body.get("tool_choice"), dict) and body["tool_choice"].get("type") == "image_generation":
            item = {"id": "ig_1", "type": "image_generation_call", "status": "completed", "result": PNG_B64,
                    "revised_prompt": "a cat"}
            yield ev({"type": "response.output_item.added", "output_index": 0, "item": {**item, "result": None}})
            yield ev({"type": "response.output_item.done", "output_index": 0, "item": item})
            out = [item]
        elif model == "gpt-tool":
            item = {"id": "fc_1", "type": "function_call", "call_id": "call_abc", "name": "get_weather", "arguments": ""}
            yield ev({"type": "response.output_item.added", "output_index": 0, "item": item})
            yield ev({"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{\"city\":\"Hue\"}"})
            yield ev({"type": "response.output_item.done", "output_index": 0, "item": {**item, "arguments": "{\"city\":\"Hue\"}"}})
            out = [{**item, "arguments": "{\"city\":\"Hue\"}"}]
        else:
            yield ev({"type": "response.reasoning_summary_text.delta", "delta": "hmm"})
            msg = {"id": "msg_1", "type": "message", "role": "assistant", "content": []}
            yield ev({"type": "response.output_item.added", "output_index": 1, "item": msg})
            for piece in ("Xin ", "chào"):
                yield ev({"type": "response.output_text.delta", "item_id": "msg_1", "delta": piece})
            done = {**msg, "content": [{"type": "output_text", "text": "Xin chào"}]}
            yield ev({"type": "response.output_item.done", "output_index": 1, "item": done})
            out = [done]
        yield ev({"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": out,
                                                             "usage": {"input_tokens": 20, "output_tokens": 4}}})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/codex/models")
async def codex_models(request: Request):
    calls["codex-models"] = calls.get("codex-models", 0) + 1
    if not request.headers.get("authorization", "").startswith("Bearer ey"):
        return JSONResponse({"detail": "Unauthorized"}, 401)
    return {"models": [{"slug": "gpt-ok"}, {"slug": "gpt-new-b"}, {"slug": "gpt-new-a"}]}


# ---------------------------------------------------------------- Gemini Web mock
gw_state = {"rotations": 0, "cookie_dead": False, "at": "xsrf-tok-123"}


@app.get("/gw/app")
async def gw_app(request: Request):
    calls["gw-app"] = calls.get("gw-app", 0) + 1
    cookie = request.headers.get("cookie", "")
    if gw_state["cookie_dead"] or "__Secure-1PSID=" not in cookie:
        return app_html_response("<html><body>signed out</body></html>")
    if "DEADPSID" in cookie:
        # mô phỏng /sorry/ (Google bắt xác minh)
        return app_html_response("<html><body>unusual traffic detected</body></html>")
    return app_html_response('<html><script>window.WIZ={"SNlM0e":"%s","cfb2h":"boq_test_1"};</script></html>' % gw_state["at"])


def app_html_response(html: str):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.post("/gw/RotateCookies")
async def gw_rotate(request: Request):
    gw_state["rotations"] += 1
    from fastapi.responses import JSONResponse as JR
    resp = JR({"ok": True})
    resp.set_cookie("__Secure-1PSIDTS", f"rot-{gw_state['rotations']}")
    return resp


@app.post("/gw/stream")
async def gw_stream(request: Request):
    calls["gw-stream"] = calls.get("gw-stream", 0) + 1
    body = (await request.body()).decode()
    last_body["gw-stream"] = body
    from urllib.parse import parse_qs
    if parse_qs(body).get("at", [""])[0] != gw_state["at"]:
        calls["gw-stale"] = calls.get("gw-stale", 0) + 1
        return app_text("stale token", 400)
    if "FAILPROMPT" in body:
        return app_text("\n)]}'\n\n[[\"wrb.fr\",null,null]]\nBardErrorInfo [42]\n")
    # Gemini trả bản tích lũy: mỗi dòng wrb.fr là câu trả lời dài hơn dòng trước
    def line(text):
        inner = json.dumps([None, None, None, None, [[None, [text]]]])
        return json.dumps([["wrb.fr", None, inner]]) + " " * 200 + "\n"
    full = "Xin chào từ Gemini Web."
    if "RICH" in body:
        full = 'Hi\n<Section title="Greeting">\nXin chào\n</Section>'
    cuts = [8, 16, len(full)]
    return app_text(")]}'\n\n" + "".join(line(full[:c]) for c in cuts))


def app_text(text: str, status: int = 200):
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(text, status_code=status)


# ---------------------------------------------------------------- PKCE authorize mock
@app.post("/auth/oauth/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") == "authorization_code":
        calls["pkce_exchange"] = calls.get("pkce_exchange", 0) + 1
        if form.get("code") != "good-code" or not form.get("code_verifier"):
            return JSONResponse({"error": "invalid_grant"}, 400)
        return {"access_token": access_token(), "refresh_token": "rt-pkce", "id_token": access_token(), "expires_in": 3600}
    calls["refresh"] = calls.get("refresh", 0) + 1
    if form.get("refresh_token") == "dead":
        return JSONResponse({"error": "invalid_grant", "error_description": "refresh token reused"}, 400)
    return {"access_token": access_token(), "refresh_token": "rt-new", "id_token": access_token(), "expires_in": 3600}
