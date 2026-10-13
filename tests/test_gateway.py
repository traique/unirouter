import time
import json

import anthropic
import httpx
import openai
import pytest


def oa(env, key="master-test"):
    return openai.OpenAI(base_url=env["base"] + "/v1", api_key=key, max_retries=0)


# ------------------------------------------------------------------ auth
def test_auth_required(env):
    r = httpx.post(env["base"] + "/v1/chat/completions", json={"model": "mk/ok", "messages": []})
    assert r.status_code == 401
    with pytest.raises(openai.AuthenticationError):
        oa(env, "wrong").chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])


def test_client_key_and_model_restriction(env):
    st = env["st"]
    raw = st.store.create_key("bot", ["mk/*"], 0)
    c = oa(env, raw)
    r = c.chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from ok"
    with pytest.raises(openai.PermissionDeniedError):
        c.chat.completions.create(model="mk2/ok", messages=[{"role": "user", "content": "hi"}])


def test_rate_limit(env):
    st = env["st"]
    raw = st.store.create_key("limited", None, 2)
    c = oa(env, raw)
    for _ in range(2):
        c.chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(openai.RateLimitError):
        c.chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])


# ------------------------------------------------------------------ chat completions
def test_chat_nonstream_usage(env):
    r = oa(env).chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from ok"
    assert r.usage.prompt_tokens == 11 and r.usage.completion_tokens == 5


def test_chat_stream(env):
    chunks = list(oa(env).chat.completions.create(model="mk/ok", stream=True, stream_options={"include_usage": True},
                                                  messages=[{"role": "user", "content": "hi"}]))
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "Hello from ok"
    assert chunks[-1].usage.completion_tokens == 5
    plain = list(oa(env).chat.completions.create(model="mk/ok", stream=True, messages=[{"role": "user", "content": "hi"}]))
    assert all(c.choices for c in plain)  # không gửi chunk usage khi client không yêu cầu


def test_chat_tool_calls_stream(env):
    chunks = list(oa(env).chat.completions.create(model="mk/tool", stream=True,
                                                  messages=[{"role": "user", "content": "weather?"}],
                                                  tools=[{"type": "function", "function": {"name": "get_weather",
                                                                                           "parameters": {"type": "object"}}}]))
    args = "".join(tc.function.arguments or "" for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or []))
    assert json.loads(args) == {"city": "Hanoi"}
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "tool_calls"


def test_json_upstream_without_stream(env):
    r = oa(env).chat.completions.create(model="mk/nostream", messages=[{"role": "user", "content": "x"}])
    assert r.choices[0].message.content == "json-reply"


# ------------------------------------------------------------------ routing / fallback
def test_combo_fallback_and_breaker(env):
    st = env["st"]
    st.store.upsert_combo({"name": "smart", "strategy": "priority", "targets": ["mk/fail500", "mk/rate429", "mk2/ok"]})
    r = httpx.post(env["base"] + "/v1/chat/completions", headers={"Authorization": "Bearer master-test"},
                   json={"model": "smart", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert r.headers["x-unirouter-target"] == "mk2/ok"
    # 429 -> tài khoản mock bị tạm khóa theo Retry-After
    cid = next(c["id"] for c in st.store.connections.values() if c["label"] == "good")
    assert 0 < st.health.wait_seconds(cid) <= 7
    st.health.reset(cid)


def test_request_error_not_fallback(env):
    env["st"].store.upsert_combo({"name": "strict", "targets": ["mk/badreq", "mk2/ok"]})
    with pytest.raises(openai.BadRequestError):
        oa(env).chat.completions.create(model="strict", messages=[{"role": "user", "content": "hi"}])


def test_model_not_found_fallback(env):
    env["st"].store.upsert_combo({"name": "nf", "targets": ["mk/nomodel", "mk2/ok"]})
    r = oa(env).chat.completions.create(model="nf", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from ok"


def test_stream_error_before_output_fallback(env):
    env["st"].store.upsert_combo({"name": "se", "targets": ["mk/streamerr", "mk2/ok"]})
    chunks = list(oa(env).chat.completions.create(model="se", stream=True, messages=[{"role": "user", "content": "hi"}]))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello from ok"


def test_round_robin(env):
    env["st"].store.upsert_combo({"name": "rr", "strategy": "round_robin", "targets": ["mk/ok", "mk2/ok"]})
    seen = set()
    for _ in range(4):
        r = httpx.post(env["base"] + "/v1/chat/completions", headers={"Authorization": "Bearer master-test"},
                       json={"model": "rr", "messages": [{"role": "user", "content": "hi"}]})
        seen.add(r.headers["x-unirouter-target"])
    assert seen == {"mk/ok", "mk2/ok"}


def test_unknown_model_uses_default(env):
    st = env["st"]
    st.store.set_settings({"default_model": "mk2/ok"})
    r = oa(env).chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from ok"
    st.store.set_settings({"default_model": ""})
    # không đặt model mặc định -> dùng "auto" (mọi provider có tài khoản)
    r = oa(env).chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content
    st.store.set_settings({"unknown_model_fallback": False})
    with pytest.raises(openai.NotFoundError):
        oa(env).chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    st.store.set_settings({"unknown_model_fallback": True})


def test_midstream_failure_reported_in_band(env):
    with pytest.raises(openai.APIError):
        list(oa(env).chat.completions.create(model="mk/midfail", stream=True,
                                             messages=[{"role": "user", "content": "hi"}]))


def test_slow_stream_keepalive_ok(env):
    import unirouter.api as api
    old = api.EARLY_WAIT
    api.EARLY_WAIT = 0.2
    try:
        chunks = list(oa(env).chat.completions.create(model="mk/slow", stream=True,
                                                      messages=[{"role": "user", "content": "hi"}]))
        assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello from slow"
    finally:
        api.EARLY_WAIT = old


# ------------------------------------------------------------------ RTK
def test_rtk_compresses_tool_output(env):
    big = "\n".join(f"src/file{i % 3}.py:{i}: match line {i}" for i in range(400))
    oa(env).chat.completions.create(model="mk/ok", messages=[
        {"role": "user", "content": "grep"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": big}])
    sent = env["mock"].last_body["ok"]["messages"][-1]["content"]
    assert len(sent) < len(big) / 3 and "matches in 3F" in sent


# ------------------------------------------------------------------ Anthropic
def test_anthropic_messages(env):
    c = anthropic.Anthropic(base_url=env["base"], api_key="master-test", max_retries=0)
    r = c.messages.create(model="mk/ok", max_tokens=100, system="be nice", messages=[{"role": "user", "content": "hi"}])
    assert r.content[0].text == "Hello from ok" and r.stop_reason == "end_turn"
    assert r.usage.output_tokens == 5


def test_anthropic_stream_tools(env):
    c = anthropic.Anthropic(base_url=env["base"], api_key="master-test", max_retries=0)
    with c.messages.stream(model="mk/tool", max_tokens=100, messages=[{"role": "user", "content": "weather"}],
                           tools=[{"name": "get_weather", "input_schema": {"type": "object"}}]) as s:
        final = s.get_final_message()
    tool = [b for b in final.content if b.type == "tool_use"][0]
    assert tool.input == {"city": "Hanoi"} and final.stop_reason == "tool_use"
    # vòng 2: gửi tool_result
    r = c.messages.create(model="mk/ok", max_tokens=100, messages=[
        {"role": "user", "content": "weather"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": tool.id, "name": "get_weather", "input": tool.input}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool.id, "content": "30C"},
                                     {"type": "text", "text": "thanks"}]}])
    sent = env["mock"].last_body["ok"]["messages"]
    assert sent[-2]["role"] == "tool" and sent[-2]["tool_call_id"] == tool.id and sent[-1]["content"] == "thanks"
    assert r.content[0].text == "Hello from ok"


def test_anthropic_error_format(env):
    c = anthropic.Anthropic(base_url=env["base"], api_key="nope", max_retries=0)
    with pytest.raises(anthropic.AuthenticationError):
        c.messages.create(model="mk/ok", max_tokens=10, messages=[{"role": "user", "content": "hi"}])


def test_count_tokens(env):
    r = httpx.post(env["base"] + "/v1/messages/count_tokens", headers={"x-api-key": "master-test"},
                   json={"model": "x", "messages": [{"role": "user", "content": "hello world"}]})
    assert r.json()["input_tokens"] > 0


# ------------------------------------------------------------------ Responses API
def test_responses_to_openai_provider(env):
    r = oa(env).responses.create(model="mk/ok", input="hi", instructions="be brief")
    assert r.output_text == "Hello from ok"
    s = list(oa(env).responses.create(model="mk/tool", input="weather", stream=True,
                                      tools=[{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]))
    done = [e for e in s if e.type == "response.completed"][0]
    fc = [o for o in done.response.output if o.type == "function_call"][0]
    assert fc.name == "get_weather" and json.loads(fc.arguments) == {"city": "Hanoi"}


# ------------------------------------------------------------------ Codex (ChatGPT)
def test_codex_chat_with_refresh(env):
    mock = env["mock"]
    st = env["st"]
    cid = next(c["id"] for c in st.store.connections.values() if c["label"] == "chatgpt-1")
    sec = st.store.connection_secret(cid)
    st.store.update_connection_secret(cid, {**sec, "expires_at": int(time.time() * 1000) + 60_000})  # sắp hết hạn
    before = mock.calls.get("refresh", 0)
    r = oa(env).chat.completions.create(model="cx/gpt-ok", messages=[{"role": "system", "content": "sys"},
                                                                      {"role": "user", "content": "chào"}])
    assert r.choices[0].message.content == "Xin chào"
    assert r.usage.prompt_tokens == 20
    assert mock.calls["refresh"] == before + 1  # token sắp hết hạn -> refresh
    sent = mock.last_body["codex:gpt-ok"]
    assert sent["body"]["instructions"] == "sys" and sent["body"]["store"] is False
    assert sent["headers"]["chatgpt-account-id"] == "acc-123"
    assert sent["headers"]["originator"] == "codex_cli_rs"
    assert set(sent["body"]) <= {"model", "input", "instructions", "tools", "tool_choice", "stream", "store",
                                 "reasoning", "service_tier", "include", "prompt_cache_key", "text", "parallel_tool_calls"}
    # lần 2 dùng token mới, không refresh lại
    oa(env).chat.completions.create(model="cx/gpt-ok", messages=[{"role": "user", "content": "x"}])
    assert mock.calls["refresh"] == before + 1


def test_codex_bare_model_and_effort_suffix(env):
    r = oa(env).chat.completions.create(model="cx/gpt-ok-high", messages=[{"role": "user", "content": "x"}])
    assert r.choices[0].message.content == "Xin chào"
    body = env["mock"].last_body["codex:gpt-ok"]["body"]
    assert body["reasoning"]["effort"] == "high"
    r = oa(env).chat.completions.create(model="gpt-ok", messages=[{"role": "user", "content": "x"}])
    assert r.choices[0].message.content == "Xin chào"


def test_codex_tool_call_stream(env):
    chunks = list(oa(env).chat.completions.create(
        model="cx/gpt-tool", stream=True, messages=[{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]))
    calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
    assert calls[0].id == "call_abc" and calls[0].function.name == "get_weather"
    sent = env["mock"].last_body["codex:gpt-tool"]["body"]
    assert sent["tools"][0] == {"type": "function", "name": "get_weather", "parameters": {"type": "object"}}


def test_codex_overload_retry(env):
    r = oa(env).chat.completions.create(model="cx/gpt-overload", messages=[{"role": "user", "content": "x"}])
    assert r.choices[0].message.content == "Xin chào"
    assert env["mock"].calls["codex:gpt-overload"] == 2


def test_codex_usage_limit_cooldown_and_fallback(env):
    st = env["st"]
    st.store.upsert_combo({"name": "gpt-main", "targets": ["cx/gpt-limit", "mk2/ok"]})
    r = httpx.post(env["base"] + "/v1/chat/completions", headers={"Authorization": "Bearer master-test"},
                   json={"model": "gpt-main", "messages": [{"role": "user", "content": "hi"}]})
    assert r.headers["x-unirouter-target"] == "mk2/ok"
    cid = next(c["id"] for c in st.store.connections.values() if c["label"] == "chatgpt-1")
    assert st.health.wait_seconds(cid) > 1000
    st.health.reset(cid)


def test_codex_responses_passthrough(env):
    r = oa(env).responses.create(model="cx/gpt-ok", input="chào")
    assert r.output_text == "Xin chào"
    events = list(oa(env).responses.create(model="cx/gpt-ok", input="chào", stream=True))
    assert events[0].type == "response.created" and events[-1].type == "response.completed"


def test_codex_anthropic(env):
    c = anthropic.Anthropic(base_url=env["base"], api_key="master-test", max_retries=0)
    r = c.messages.create(model="cx/gpt-ok", max_tokens=100, messages=[{"role": "user", "content": "hi"}])
    assert r.content[0].text == "Xin chào"


# ------------------------------------------------------------------ models + admin
def test_models_list(env):
    ids = [m.id for m in oa(env).models.list()]
    assert "mk/ok" in ids and "cx/gpt-ok" in ids and "smart" in ids


def test_admin_flow(env):
    with httpx.Client(base_url=env["base"]) as c:
        assert c.get("/admin/api/overview").status_code == 401
        assert c.post("/admin/api/login", json={"username": "admin", "password": "bad"}).status_code == 401
        assert c.post("/admin/api/login", json={"username": "admin", "password": "pw"}).status_code == 200
        h = {"X-Requested-With": "unirouter"}
        ov = c.get("/admin/api/overview").json()
        assert ov["stats"]["total"]["n"] > 0
        provs = c.get("/admin/api/providers").json()["data"]
        mock = next(p for p in provs if p["id"] == "mock")
        assert mock["connections"][0]["key_hint"] == "••••"
        assert c.post("/admin/api/keys", json={"name": "x"}).status_code == 403  # thiếu header CSRF
        key = c.post("/admin/api/keys", json={"name": "x", "rpm": 5}, headers=h).json()["key"]
        assert key.startswith("sk-uni-")
        cid = mock["connections"][0]["id"]
        t = c.post(f"/admin/api/connections/{cid}/test", json={"model": "ok"}, headers=h).json()
        assert t["ok"] and t["reply"] == "Hello from ok"
        assert c.post("/admin/api/providers/mock/fetch-models", headers=h).json()["models"] == ["m1", "m2"]
        pg = c.post("/admin/api/playground", json={"model": "smart", "message": "hi"}, headers=h).json()
        assert pg["ok"] and pg["reply"]
        combos = c.get("/admin/api/combos").json()["data"]
        assert any(x["name"] == "smart" for x in combos)
        imp = c.post("/admin/api/codex/import", headers=h, json={"text": json.dumps(
            {"tokens": {"access_token": env["mock"].access_token(), "refresh_token": "rt-x", "account_id": "acc-123"}})})
        assert imp.status_code == 200, imp.text
        # cùng account_id -> cập nhật, không tạo bản trùng
        assert sum(1 for x in env["st"].store.connections.values() if x["provider_id"] == "codex") == 1
        s = c.post("/admin/api/settings", json={"rtk_enabled": False, "breaker_threshold": 5}, headers=h)
        assert s.status_code == 200 and env["st"].health.threshold == 5
        c.post("/admin/api/settings", json={"rtk_enabled": True}, headers=h)


def test_usage_logged(env):
    env["st"].usage.flush()
    rows = env["st"].store.q("SELECT COUNT(*) n FROM usage WHERE provider='mock2'")
    assert rows[0]["n"] > 0


def test_dead_refresh_token_blocks_account(env):
    st = env["st"]
    c = st.store.add_connection("codex", "dead-acc", {"access_token": None, "refresh_token": "dead", "expires_at": 0},
                                {"account_id": "acc-dead"})
    # tắt tài khoản tốt để buộc dùng tài khoản hỏng
    good = next(x for x in st.store.connections.values() if x["label"] == "chatgpt-1")
    st.store.update_connection(good["id"], {"enabled": False})
    try:
        with pytest.raises(openai.APIStatusError) as e:
            oa(env).chat.completions.create(model="cx/gpt-ok", messages=[{"role": "user", "content": "x"}])
        assert e.value.status_code in (401, 429, 503)
        assert st.health.wait_seconds(c["id"]) > 1000
        assert "refresh" in (st.store.connections[c["id"]]["last_error"] or "").lower()
    finally:
        st.store.update_connection(good["id"], {"enabled": True})
        st.store.delete_connection(c["id"])


def test_codex_lite_model_payload(env):
    st = env["st"]
    p = st.store.providers["codex"]
    st.store.upsert_provider({"id": "codex", "options": {**p["options"], "lite_models": ["gpt-ok"]}})
    try:
        oa(env).chat.completions.create(model="cx/gpt-ok", messages=[{"role": "system", "content": "S"},
                                                                      {"role": "user", "content": "x"}],
                                        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}])
        sent = env["mock"].last_body["codex:gpt-ok"]
        b = sent["body"]
        assert sent["headers"]["x-openai-internal-codex-responses-lite"] == "true"
        assert b["input"][0]["type"] == "additional_tools" and b["input"][0]["tools"][0]["name"] == "f"
        assert b["input"][1]["content"][0]["text"] == "S" and b["instructions"] == ""
        assert b["reasoning"]["context"] == "all_turns"
    finally:
        st.store.upsert_provider({"id": "codex", "options": p["options"]})


def test_anthropic_writer_interleaved_tools():
    from unirouter.formats.anthropic import AnthropicWriter
    w = AnthropicWriter("m")
    for ev in [{"type": "tool_start", "index": 0, "id": "a", "name": "f"}, {"type": "tool_start", "index": 1, "id": "b", "name": "g"},
               {"type": "tool_args", "index": 0, "args": "{\"x\":1}"}, {"type": "tool_args", "index": 1, "args": "{\"y\":2}"},
               {"type": "finish", "reason": "tool_calls"}]:
        w.feed(ev)
    frames = "".join(w.finish())
    assert '{\\"x\\":1}' in frames and '{\\"y\\":2}' in frames
    assert [c["input"] for c in w.result()["content"]] == [{"x": 1}, {"y": 2}]


def test_responses_to_chat_keeps_parallel_calls_grouped():
    from unirouter.formats.responses import responses_to_chat
    chat = responses_to_chat({"model": "m", "input": [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "A", "name": "f", "arguments": "{}"},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "B", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "A", "output": "1"},
        {"type": "function_call_output", "call_id": "B", "output": "2"}]})
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["user", "assistant", "tool", "tool"]
    assert [c["id"] for c in chat["messages"][1]["tool_calls"]] == ["A", "B"]


def test_least_latency_puts_failing_target_last():
    from unirouter.health import Health
    h = Health()
    for _ in range(5):
        h.record_latency("bad", 10, False)
    h.record_latency("good", 500, True)
    assert sorted(["bad", "good"], key=h.latency) == ["good", "bad"]


def test_restricted_key_no_default_fallback(env):
    st = env["st"]
    st.store.set_settings({"default_model": "mk2/ok"})
    raw = st.store.create_key("restricted", ["gpt-*"], 0)
    try:
        with pytest.raises(openai.NotFoundError):
            oa(env, raw).chat.completions.create(model="gpt-whatever", messages=[{"role": "user", "content": "x"}])
    finally:
        st.store.set_settings({"default_model": ""})


def test_ssrf_guard():
    import asyncio
    from unirouter.providers.codex import is_public_url
    assert not asyncio.run(is_public_url("http://127.0.0.1/x.png"))
    assert not asyncio.run(is_public_url("http://169.254.169.254/latest"))
    assert not asyncio.run(is_public_url("file:///etc/passwd"))


def test_non_ascii_password_and_key(env):
    r = httpx.post(env["base"] + "/admin/api/login", json={"username": "admin", "password": "mật khẩu"})
    assert r.status_code == 401
    r = httpx.get(env["base"] + "/v1/models", headers={"Authorization": "Bearer khóa".encode()})
    assert r.status_code == 401


def test_codex_client_disconnect_aborts_upstream(env):
    import unirouter.api as api
    old = api.EARLY_WAIT
    api.EARLY_WAIT = 0.1
    try:
        with httpx.Client(timeout=httpx.Timeout(5, read=0.8)) as c:
            with pytest.raises(httpx.ReadTimeout):
                with c.stream("POST", env["base"] + "/v1/chat/completions", headers={"Authorization": "Bearer master-test"},
                              json={"model": "cx/gpt-slow", "stream": True, "messages": [{"role": "user", "content": "x"}]}) as r:
                    for _ in r.iter_lines():
                        pass
        time.sleep(3.5)
        assert env["mock"].calls.get("gpt-slow-finished", 0) == 0  # upstream đã bị hủy, không chạy tới cuối
    finally:
        api.EARLY_WAIT = old


def test_load_dotenv(tmp_path, monkeypatch):
    from unirouter.config import load_dotenv
    p = tmp_path / ".env"
    p.write_text('# c\nUNI_A=1\nexport UNI_B="x y"\nUNI_C=\'q+/=\'\nUNI_D=keep\n\nbad line\n')
    monkeypatch.setenv("UNI_D", "env-wins")
    for k in ("UNI_A", "UNI_B", "UNI_C"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(p, tmp_path / "missing.env")
    import os
    assert (os.environ["UNI_A"], os.environ["UNI_B"], os.environ["UNI_C"], os.environ["UNI_D"]) == ("1", "x y", "q+/=", "env-wins")
    for k in ("UNI_A", "UNI_B", "UNI_C"):
        monkeypatch.delenv(k, raising=False)


def test_admin_quick_provider(env):
    with httpx.Client(base_url=env["base"]) as c:
        c.post("/admin/api/login", json={"username": "admin", "password": "pw"})
        h = {"X-Requested-With": "unirouter"}
        r = c.post("/admin/api/providers/quick", headers=h, json={
            "name": "Mạng Đẹp API", "base_url": env["up"] + "/oai/v1/chat/completions", "models": "", "api_key": "good"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["alias"] == "mang-dep-api" and d["models"] == ["m1", "m2"] and d["test"]["ok"]
        p = env["st"].store.providers["mang-dep-api"]
        assert p["base_url"] == env["up"] + "/oai/v1"
        r2 = c.post("/admin/api/providers/quick", headers=h, json={
            "name": "Mạng Đẹp API", "base_url": env["up"] + "/oai/v1", "models": "ok", "api_key": "good"}).json()
        assert r2["alias"] == "mang-dep-api-2" and r2["models"] == ["ok"]
        assert c.post("/admin/api/providers/quick", headers=h, json={"name": "x", "base_url": "abc"}).status_code == 400
    r = oa(env).chat.completions.create(model="mang-dep-api-2/ok", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from ok"


def test_auto_drop_stream_options(env):
    st = env["st"]
    for c in st.store.connections.values():
        st.health.reset(c["id"])
    r = oa(env).chat.completions.create(model="mk/nostreamopt", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Hello from nostreamopt"
    assert st.store.providers["mock"]["options"].get("stream_usage") is False  # đã ghi nhớ
    st.store.upsert_provider({"id": "mock", "options": {}})


def test_openrouter_auto_headers():
    from unirouter.providers.openai_compat import headers_for
    h = headers_for({"base_url": "https://openrouter.ai/api/v1", "headers": {}}, {"api_key": "k"})
    assert h["X-Title"] == "UniRouter" and h["Authorization"] == "Bearer k"
    assert "X-Title" not in headers_for({"base_url": "https://api.groq.com/openai/v1"}, {"api_key": "k"})


def test_auto_model(env):
    st = env["st"]
    for c in st.store.connections.values():
        st.health.reset(c["id"])
    targets, _ = st.router.resolve("auto")
    labels = [t.label for t in targets]
    assert labels[0] == "cx/gpt-ok" and "mk2/ok" in labels
    assert "auto" in [m.id for m in oa(env).models.list()]
    r = oa(env).chat.completions.create(model="auto", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "Xin chào"
    # Claude Code gửi tên model Claude -> tự chạy qua auto
    c = anthropic.Anthropic(base_url=env["base"], api_key="master-test", max_retries=0)
    assert c.messages.create(model="claude-unknown-model", max_tokens=50,
                             messages=[{"role": "user", "content": "hi"}]).content[0].text == "Xin chào"


def test_add_key_autofetch_models_free_first(env):
    st = env["st"]
    st.store.upsert_provider({"id": "freecat", "alias": "fc", "name": "Free Cat", "type": "openai",
                              "base_url": env["up"] + "/oai/v1", "models": [],
                              "options": {"models_url": env["up"] + "/catalog/models"}})
    with httpx.Client(base_url=env["base"]) as c:
        c.post("/admin/api/login", json={"username": "admin", "password": "pw"})
        r = c.post("/admin/api/providers/freecat/connections", headers={"X-Requested-With": "unirouter"},
                   json={"api_key": "good"}).json()
    assert r["models"] == 3
    assert st.store.providers["freecat"]["models"] == ["alpha-free", "ok", "zeta-paid"]
    assert r["test"] is not None  # đã gọi thử model đầu tiên
    provs = None
    with httpx.Client(base_url=env["base"]) as c:
        c.post("/admin/api/login", json={"username": "admin", "password": "pw"})
        provs = {p["id"]: p for p in c.get("/admin/api/providers").json()["data"]}
    assert provs["github"]["free"] and provs["github"]["signup_url"].startswith("https://github.com")
    assert provs["opencode-zen"]["base_url"] == "https://opencode.ai/zen/v1"


# ------------------------------------------------------------------ embeddings / images
def test_embeddings_explicit_and_usage(env):
    r = oa(env).embeddings.create(model="mk/text-embed-small", input=["a", "b"])
    assert len(r.data) == 2 and r.data[0].embedding == [0.1, 0.2, 0.3]
    assert r.model == "mk/text-embed-small"


def test_embeddings_auto_fallback_and_input_type(env):
    st = env["st"]
    st.store.upsert_provider({"id": "mock", "options": {**st.store.providers["mock"]["options"],
                                                        "embedding_model": "emb-fail"}})
    st.store.upsert_provider({"id": "mock2", "options": {**st.store.providers["mock2"]["options"],
                                                         "embedding_model": "emb-needtype"}})
    try:
        r = httpx.post(env["base"] + "/v1/embeddings", headers={"Authorization": "Bearer master-test"},
                       json={"input": "xin chào"})
        assert r.status_code == 200, r.text
        assert r.headers["x-unirouter-target"] == "mk2/emb-needtype"
        assert env["mock"].last_body["emb:emb-needtype"]["input_type"] == "query"
        # client gửi tên model OpenAI -> tự chuyển sang auto embeddings
        r = oa(env).embeddings.create(model="text-embedding-3-small", input="hi")
        assert r.model == "mk2/emb-needtype"
    finally:
        for pid in ("mock", "mock2"):
            opts = dict(st.store.providers[pid]["options"])
            opts.pop("embedding_model", None)
            st.store.upsert_provider({"id": pid, "options": opts})
        env["st"].health.conns.clear()


def test_embeddings_none_configured(env):
    r = httpx.post(env["base"] + "/v1/embeddings", headers={"Authorization": "Bearer master-test"},
                   json={"model": "auto", "input": "x"})
    assert r.status_code == 503 and "embedding" in r.json()["error"]["message"]


def test_image_generation_url_and_file(env):
    r = oa(env).images.generate(model="mk/img-ok", prompt="a cat")
    url = r.data[0].url
    assert url.startswith(env["base"] + "/v1/images/files/") and r.data[0].b64_json
    img = httpx.get(url)
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG") and img.headers["content-type"] == "image/png"
    r = oa(env).images.generate(model="mk/img-ok", prompt="a cat", response_format="b64_json")
    assert r.data[0].b64_json and not r.data[0].url
    assert httpx.get(env["base"] + "/v1/images/files/..%2Fsecret.key").status_code == 404


def test_image_generation_retry_without_response_format_and_upstream_url(env):
    r = oa(env).images.generate(model="mk/img-noformat", prompt="x", response_format="url")
    assert r.data[0].url and not r.data[0].b64_json
    r = oa(env).images.generate(model="mk/img-url", prompt="x")
    assert r.data[0].url == "https://cdn.example.com/a.png"


def test_image_generation_auto_uses_codex_last(env):
    st = env["st"]
    st.store.upsert_provider({"id": "mock", "options": {**st.store.providers["mock"]["options"], "image_model": "img-fail"}})
    try:
        r = httpx.post(env["base"] + "/v1/images/generations", headers={"Authorization": "Bearer master-test"},
                       json={"prompt": "một con mèo", "size": "1024x1024"})
        assert r.status_code == 200, r.text
        assert r.headers["x-unirouter-target"] == "cx/gpt-image"
        assert r.json()["data"][0]["revised_prompt"] == "a cat"
        sent = [v for k, v in env["mock"].last_body.items() if k.startswith("codex:")]
        assert any(b["body"].get("tools", [{}])[0].get("type") == "image_generation" for b in sent)
    finally:
        opts = dict(st.store.providers["mock"]["options"])
        opts.pop("image_model", None)
        st.store.upsert_provider({"id": "mock", "options": opts})
        st.health.conns.clear()


def test_chat_auto_skips_embedding_models(env):
    from unirouter.router import Router
    p = {"models": ["text-embedding-3-small", "gpt-x"], "options": {}, "type": "openai"}
    assert Router.kind_model(p, "chat") == "gpt-x"
    assert Router.kind_model(p, "embeddings") == "text-embedding-3-small"
    assert Router.kind_model({**p, "options": {"embedding_model": ""}}, "embeddings") is None


def test_models_list_includes_embedding_defaults(env):
    ids = [m.id for m in oa(env).models.list().data]
    assert "auto" in ids


def test_admin_backup_and_key_stats(env):
    c = httpx.Client(base_url=env["base"])
    c.post("/admin/api/login", json={"username": "admin", "password": "pw"})
    r = c.get("/admin/api/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")
    keys = c.get("/admin/api/keys").json()["data"]
    assert all("usage_24h" in k for k in keys)
    r = c.post("/admin/api/playground/image", json={"model": "mk/img-ok", "prompt": "cat"},
               headers={"X-Requested-With": "unirouter"}).json()
    assert r["ok"] and r["images"][0]["url"]
    r = c.post("/admin/api/playground/embed", json={"model": "mk/x-embed", "input": "hi"},
               headers={"X-Requested-With": "unirouter"}).json()
    assert r["ok"] and r["dims"] == 3


def test_removes_old_voice_providers():
    import json
    import sqlite3
    import tempfile
    from unirouter.db import Store
    path = tempfile.mktemp(suffix=".db")
    Store(path)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO providers (id,alias,name,type,base_url,models,headers,options,note,enabled,builtin,created_at)"
                " VALUES ('edge-tts','edge','Edge','edge','https://x','[]','{}','{}','',1,1,0)")
    con.execute("INSERT INTO connections (id,provider_id,label,secret_enc,meta,enabled,priority,created_at)"
                " VALUES ('c1','edge-tts','x','x',?,1,0,0)", (json.dumps({}),))
    con.commit()
    con.close()
    st = Store(path)
    assert "edge-tts" not in st.providers and not st.q("SELECT 1 FROM connections WHERE provider_id='edge-tts'")


# ------------------------------------------------------------------ hồi quy sau review
def test_image_bad_n_is_400_and_does_not_lock_accounts(env):
    st = env["st"]
    for _ in range(4):
        r = httpx.post(env["base"] + "/v1/images/generations", headers={"Authorization": "Bearer master-test"},
                       json={"model": "cx/gpt-image", "prompt": "x", "n": "two"})
        assert r.status_code == 400
    r = httpx.post(env["base"] + "/v1/images/generations", headers={"Authorization": "Bearer master-test"},
                   json={"prompt": "x", "size": "huge"})
    assert r.status_code == 400
    cx = [c for c in st.store.provider_connections("codex")][0]
    assert st.health.available(cx["id"])


def test_auth_checked_before_body(env):
    r = httpx.post(env["base"] + "/v1/embeddings", json={"input": ""})
    assert r.status_code == 401
    r = httpx.post(env["base"] + "/v1/images/generations", json={})
    assert r.status_code == 401
    r = httpx.post(env["base"] + "/admin/api/login", json=[])
    assert r.status_code == 400


def test_combo_named_auto_not_used_for_embeddings(env):
    st = env["st"]
    st.store.upsert_combo({"name": "auto", "strategy": "priority", "targets": ["mk/ok"]})
    try:
        r = httpx.post(env["base"] + "/v1/embeddings", headers={"Authorization": "Bearer master-test"},
                       json={"input": "hi"})
        assert r.status_code == 503  # không có model embedding -> không gửi nhầm sang model chat
    finally:
        st.store.delete_combo("auto")


def test_key_stats_by_id(env):
    st = env["st"]
    a = st.store.create_key("dup", None, 0)
    st.store.create_key("dup", None, 0)
    oa(env, a).chat.completions.create(model="mk/ok", messages=[{"role": "user", "content": "hi"}])
    c = httpx.Client(base_url=env["base"])
    c.post("/admin/api/login", json={"username": "admin", "password": "pw"})
    dups = [k for k in c.get("/admin/api/keys").json()["data"] if k["name"] == "dup"]
    assert sorted(k["usage_24h"]["n"] for k in dups) == [0, 1]


def test_expired_image_not_served(env):
    import os
    import time as _t
    from unirouter import images
    name = images.save_b64("iVBORw0KGgo=")
    assert images.path_of(name)
    old = _t.time() - images.MAX_AGE - 10
    os.utime(images.DIR / name, (old, old))
    assert images.path_of(name) is None
    assert httpx.get(env["base"] + f"/v1/images/files/{name}").status_code == 404
