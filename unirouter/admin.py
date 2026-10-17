"""Trang quản trị /admin và API /admin/api/*."""
from __future__ import annotations

import hmac
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config
from .db import now_ms
from .formats.chat import ChatWriter
from .providers import codex_auth
from .providers.base import CallContext, ProviderError, Target
from .router import GatewayError

router = APIRouter()
COOKIE = "uni_admin"
SESSION_MAX_AGE = 12 * 3600
_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="admin-session")
_fails: dict[str, list[float]] = {}
HTML_PATH = Path(__file__).parent / "static" / "admin.html"


def S(request: Request):
    return request.app.state.app


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_admin(request: Request) -> None:
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Chưa đặt ADMIN_PASSWORD - trang quản trị đang tắt")
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Cần đăng nhập")
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature as e:
        raise HTTPException(401, "Phiên đăng nhập hết hạn") from e
    if request.method != "GET" and request.headers.get("x-requested-with") != "unirouter":
        raise HTTPException(403, "Thiếu header chống CSRF")


def _secure(request: Request) -> bool:
    if config.COOKIE_SECURE in ("true", "1"):
        return True
    if config.COOKIE_SECURE in ("false", "0"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 12 else "••••"


# ------------------------------------------------------------------ page + auth
@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"), headers={
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                   "img-src 'self' data: blob: https: http:; media-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    })


@router.post("/admin/api/login")
async def login(request: Request):
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Chưa đặt biến môi trường ADMIN_PASSWORD")
    ip = client_ip(request)
    now = time.time()
    for k in list(_fails):  # dọn mục cũ để dict không phình
        _fails[k] = [t for t in _fails[k] if t > now - 300]
        if not _fails[k]:
            del _fails[k]
    # Giới hạn theo IP và giới hạn tổng (IP có thể bị giả qua proxy)
    if len(_fails.get(ip, [])) >= 5 or sum(len(v) for v in _fails.values()) >= 30:
        raise HTTPException(429, "Sai quá nhiều lần, thử lại sau 5 phút")
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise HTTPException(400, "Body phải là JSON {username, password}")
    ok = hmac.compare_digest(str(data.get("username", "")).encode(), config.ADMIN_USERNAME.encode()) & \
        hmac.compare_digest(str(data.get("password", "")).encode(), config.ADMIN_PASSWORD.encode())
    if not ok:
        _fails.setdefault(ip, []).append(now)
        raise HTTPException(401, "Sai tài khoản hoặc mật khẩu")
    _fails.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _signer.dumps(uuid.uuid4().hex), max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="strict", secure=_secure(request), path="/")
    return resp


@router.post("/admin/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/admin/api/me")
async def me(request: Request):
    try:
        require_admin(request)
        return {"authenticated": True, "version": config.VERSION}
    except HTTPException as e:
        return {"authenticated": False, "enabled": bool(config.ADMIN_PASSWORD), "reason": e.detail}


# ------------------------------------------------------------------ overview
def connection_view(st, c: dict[str, Any]) -> dict[str, Any]:
    h = st.health.conn(c["id"])
    now = time.time()
    wait = max(0.0, max(h.cooldown_until, h.open_until) - now)
    view = {k: c[k] for k in ("id", "provider_id", "label", "enabled", "priority", "last_error", "last_used_at", "created_at")}
    view["meta"] = {k: v for k, v in (c.get("meta") or {}).items() if k in ("email", "plan", "account_id")}
    view["blocked_for"] = int(wait)
    view["block_reason"] = h.last_reason if wait else ""
    try:
        secret = st.store.connection_secret(c["id"])
    except Exception:
        secret = {}
    if "api_key" in secret:
        view["key_hint"] = mask(secret.get("api_key"))
    if "access_token" in secret or "refresh_token" in secret:
        view["expires_at"] = secret.get("expires_at")
        view["has_refresh"] = bool(secret.get("refresh_token"))
    if not c["enabled"]:
        view["health"] = "disabled"
    elif wait:
        view["health"] = "cooldown"
    elif c.get("last_error"):
        view["health"] = "error"
    else:
        view["health"] = "ok"
    return view


@router.get("/admin/api/overview")
async def overview(request: Request, hours: int = 24):
    require_admin(request)
    st = S(request)
    stats = st.usage.stats(max(1, min(hours, 24 * 30)))
    conns = list(st.store.connections.values())
    return {
        "version": config.VERSION,
        "stats": stats,
        "counts": {
            "providers": sum(1 for p in st.store.providers.values() if p["enabled"] and st.store.provider_connections(p["id"])),
            "connections": len(conns),
            "healthy": sum(1 for c in conns if connection_view(st, c)["health"] == "ok"),
            "combos": len(st.store.combos),
            "keys": len(st.store.keys),
        },
        "warnings": warnings(st),
    }


def warnings(st) -> list[str]:
    out = []
    if not config.SECRET_FROM_ENV:
        out.append("SECRET_KEY đang tự sinh và lưu trong data/secret.key. Nên đặt biến môi trường SECRET_KEY "
                   "để token đã mã hóa không mất khi thư mục data bị xóa.")
    if not st.store.keys and not config.MASTER_API_KEY:
        out.append("Chưa có API key nào - client chưa gọi được /v1. Tạo key ở tab API Keys.")
    if not st.store.connections:
        out.append("Chưa kết nối provider nào. Vào tab Providers để thêm API key hoặc đăng nhập ChatGPT.")
    return out


# ------------------------------------------------------------------ providers
@router.get("/admin/api/providers")
async def list_providers(request: Request):
    require_admin(request)
    st = S(request)
    out = []
    for p in st.store.providers.values():
        item = {k: p.get(k) for k in ("id", "alias", "name", "type", "base_url", "models", "headers", "options", "note",
                                      "enabled", "builtin", "signup_url", "free")}
        item["connections"] = [connection_view(st, c) for c in st.store.provider_connections(p["id"])]
        item["kinds"] = {k: st.router.kind_model(p, k) for k in ("chat", "embeddings", "images")}
        item["latency"] = {m: round(st.health.latency(f"{p['id']}/{m}")) for m in p["models"]
                           if 0 < st.health.latency(f"{p['id']}/{m}") < 1e8}
        out.append(item)
    return {"data": out}


@router.post("/admin/api/providers")
async def save_provider(request: Request):
    require_admin(request)
    try:
        p = S(request).store.upsert_provider(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "id": p["id"]}


def _slug(name: str) -> str:
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:24] or "api"


@router.post("/admin/api/providers/quick")
async def quick_provider(request: Request):
    """Thêm nhanh provider tương thích OpenAI: chỉ cần tên, base URL, model, API key."""
    require_admin(request)
    st = S(request)
    data = await request.json()
    name = str(data.get("name") or "").strip()
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
    api_key = str(data.get("api_key") or "").strip()
    raw_models = data.get("models") or ""
    if isinstance(raw_models, str):
        raw_models = raw_models.replace(",", "\n").split("\n")
    models = [str(m).strip() for m in raw_models if str(m).strip()]
    if not name:
        raise HTTPException(400, "Nhập tên provider")
    if not base_url.startswith(("http://", "https://")) or len(base_url) < 12:
        raise HTTPException(400, "Base URL phải bắt đầu bằng http:// hoặc https://")

    base = _slug(name)
    pid, n = base, 2
    while pid in st.store.providers or pid in st.store.alias_map:
        pid, n = f"{base}-{n}", n + 1
    try:
        st.store.upsert_provider({"id": pid, "alias": pid, "name": name, "type": "openai", "base_url": base_url,
                                  "models": models})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    conn = st.store.add_connection(pid, name, {"api_key": api_key})

    error = None
    if not models:  # tự lấy danh sách model từ provider
        try:
            models = await st.providers["openai"].list_models(st.store.providers[pid],
                                                               {**conn, "_secret": {"api_key": api_key}})
            if models:
                st.store.upsert_provider({"id": pid, "models": models})
        except ProviderError as e:
            error = e.message
        except Exception as e:  # noqa: BLE001
            error = str(e)
    test = None
    if models:
        test = await run_test(st, st.store.providers[pid], {**conn, "_secret": {"api_key": api_key}}, models[0])
    return {"ok": True, "id": pid, "alias": pid, "models": models, "error": error, "test": test}


@router.delete("/admin/api/providers/{pid}")
async def delete_provider(pid: str, request: Request):
    require_admin(request)
    try:
        S(request).store.delete_provider(pid)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.post("/admin/api/providers/{pid}/connections")
async def add_connection(pid: str, request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    p = st.store.providers.get(pid)
    if not p:
        raise HTTPException(404, "Không có provider này")
    if p["type"] == "codex":
        raise HTTPException(400, "ChatGPT dùng đăng nhập thiết bị hoặc nhập auth.json")
    keys = [k.strip() for k in str(data.get("api_key") or "").replace(",", "\n").split("\n") if k.strip()]
    if not keys:
        raise HTTPException(400, "Nhập ít nhất 1 API key (mỗi dòng 1 key để thêm nhiều)")
    label = str(data.get("label") or p["name"]).strip()
    first = None
    for i, k in enumerate(keys):
        c = st.store.add_connection(pid, label if len(keys) == 1 else f"{label} #{i + 1}", {"api_key": k},
                                    priority=int(data.get("priority") or 0))
        first = first or c
    error = None
    if not st.store.providers[pid]["models"]:  # provider chưa có model -> tự lấy bằng key vừa nhập
        try:
            models = await st.providers["openai"].list_models(st.store.providers[pid],
                                                               {**first, "_secret": {"api_key": keys[0]}})
            if models:
                st.store.upsert_provider({"id": pid, "models": models})
        except ProviderError as e:
            error = e.message
        except Exception as e:  # noqa: BLE001
            error = str(e)
    prov = st.store.providers[pid]
    test = None
    if prov["models"]:
        model = (prov.get("options") or {}).get("auto_model") or prov["models"][0]
        test = await run_test(st, prov, {**first, "_secret": {"api_key": keys[0]}}, model)
    return {"ok": True, "added": len(keys), "models": len(prov["models"]), "error": error, "test": test}


@router.patch("/admin/api/connections/{cid}")
async def update_connection(cid: str, request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    try:
        st.store.update_connection(cid, data)
    except KeyError as e:
        raise HTTPException(404, "Không có kết nối này") from e
    if data.get("enabled"):
        st.health.reset(cid)
    return {"ok": True}


@router.delete("/admin/api/connections/{cid}")
async def delete_connection(cid: str, request: Request):
    require_admin(request)
    st = S(request)
    st.store.delete_connection(cid)
    st.health.reset(cid)
    return {"ok": True}


@router.post("/admin/api/connections/{cid}/reset")
async def reset_connection(cid: str, request: Request):
    require_admin(request)
    st = S(request)
    st.health.reset(cid)
    st.store.set_connection_error(cid, None)
    return {"ok": True}


def _conn_with_secret(st, cid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    c = st.store.connections.get(cid)
    if not c:
        raise HTTPException(404, "Không có kết nối này")
    return st.store.providers[c["provider_id"]], {**c, "_secret": st.store.connection_secret(cid)}


@router.post("/admin/api/connections/{cid}/test")
async def test_connection(cid: str, request: Request):
    require_admin(request)
    st = S(request)
    provider, conn = _conn_with_secret(st, cid)
    data = await request.json() if (await request.body()) else {}
    model = data.get("model") or (provider.get("options") or {}).get("auto_model") or \
        (provider["models"][0] if provider["models"] else "")
    if not model:
        raise HTTPException(400, "Provider chưa có model nào")
    return await run_test(st, provider, conn, model)


async def run_test(st, provider: dict[str, Any], conn: dict[str, Any], model: str) -> dict[str, Any]:
    cid = conn["id"]
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly: OK"}], "max_tokens": 16}
    ctx = CallContext(body, False, "test")
    t0 = time.monotonic()
    writer = ChatWriter(model)
    try:
        it = await st.providers[provider["type"]].open(Target(provider, model, f"{provider['alias']}/{model}"), conn, ctx)
        async for ev in it:
            writer.feed(ev)
    except ProviderError as e:
        st.store.set_connection_error(cid, f"[{e.status}] {e.message}"[:300])
        return {"ok": False, "status": e.status, "error": e.message, "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "ms": int((time.monotonic() - t0) * 1000)}
    st.store.set_connection_error(cid, None)
    st.health.reset(cid)
    res = writer.result()
    return {"ok": True, "model": model, "reply": (res["choices"][0]["message"].get("content") or "")[:200],
            "ms": int((time.monotonic() - t0) * 1000)}


@router.post("/admin/api/providers/{pid}/fetch-models")
async def fetch_models(pid: str, request: Request):
    require_admin(request)
    st = S(request)
    p = st.store.providers.get(pid)
    if not p:
        raise HTTPException(404, "Không có provider này")
    conns = [c for c in st.store.provider_connections(pid) if c["enabled"]]
    if not conns:
        raise HTTPException(400, "Cần thêm ít nhất 1 tài khoản/API key trước")
    conn = {**conns[0], "_secret": st.store.connection_secret(conns[0]["id"])}
    try:
        models = await st.providers[p["type"]].list_models(p, conn)
    except ProviderError as e:
        raise HTTPException(502, f"Không lấy được danh sách model: {e.message}") from e
    return {"models": models}


@router.get("/admin/api/connections/{cid}/quota")
async def codex_quota(cid: str, request: Request):
    require_admin(request)
    st = S(request)
    c = st.store.connections.get(cid)
    if not c or st.store.providers[c["provider_id"]]["type"] != "codex":
        raise HTTPException(400, "Chỉ hỗ trợ tài khoản ChatGPT")
    try:
        return await codex_auth.fetch_usage(st.store, c)
    except ProviderError as e:
        raise HTTPException(502, e.message) from e


# ------------------------------------------------------------------ codex login
@router.post("/admin/api/codex/device/start")
async def codex_device_start(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json() if (await request.body()) else {}
    pid = data.get("provider_id") or "codex"
    try:
        info = await codex_auth.device_start()
    except ProviderError as e:
        raise HTTPException(502, e.message) from e
    login_id = uuid.uuid4().hex
    st.store.x("INSERT INTO device_logins (id,provider_id,device_auth_id,user_code,interval_seconds,label,status,expires_at,created_at)"
               " VALUES (?,?,?,?,?,?,'pending',?,?)",
               (login_id, pid, info["device_auth_id"], info["user_code"], info["interval"],
                str(data.get("label") or "")[:100], now_ms() + 15 * 60_000, now_ms()))
    return {"login_id": login_id, "user_code": info["user_code"], "verification_url": info["verification_url"],
            "interval": info["interval"]}


@router.post("/admin/api/codex/device/poll")
async def codex_device_poll(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    rows = st.store.q("SELECT * FROM device_logins WHERE id=?", (str(data.get("login_id") or ""),))
    if not rows:
        raise HTTPException(404, "Phiên đăng nhập không tồn tại")
    row = dict(rows[0])
    if row["status"] != "pending":
        return {"status": row["status"]}
    if row["expires_at"] < now_ms():
        st.store.x("UPDATE device_logins SET status='expired' WHERE id=?", (row["id"],))
        return {"status": "expired"}
    try:
        tokens = await codex_auth.device_poll(row["device_auth_id"], row["user_code"])
    except ProviderError as e:
        st.store.x("UPDATE device_logins SET status='failed' WHERE id=?", (row["id"],))
        raise HTTPException(502, e.message) from e
    if tokens is None:
        return {"status": "pending"}
    cid = save_codex_account(st, row["provider_id"], tokens, row["label"])
    st.store.x("UPDATE device_logins SET status='completed' WHERE id=?", (row["id"],))
    st.store.x("DELETE FROM device_logins WHERE created_at < ?", (now_ms() - 86400_000,))
    return {"status": "completed", "connection_id": cid}


def save_codex_account(st, pid: str, tokens: dict[str, Any], label: str | None) -> str:
    info = codex_auth.account_info(tokens)
    meta = {k: v for k, v in info.items() if k in ("account_id", "email", "plan")}
    if tokens.get("account_id") and not meta.get("account_id"):
        meta["account_id"] = tokens.pop("account_id")
    tokens.pop("account_id", None)
    # Cùng tài khoản đăng nhập lại -> cập nhật token thay vì thêm bản trùng
    for c in st.store.provider_connections(pid):
        if meta.get("account_id") and c["meta"].get("account_id") == meta["account_id"] and \
                (not meta.get("email") or c["meta"].get("email") == meta.get("email")):
            st.store.update_connection_secret(c["id"], tokens, {**c["meta"], **meta})
            st.store.update_connection(c["id"], {"enabled": True})
            st.health.reset(c["id"])
            return c["id"]
    name = label or meta.get("email") or f"ChatGPT {time.strftime('%d/%m %H:%M')}"
    if meta.get("plan") and label is None:
        name = f"{name} ({meta['plan']})"
    return st.store.add_connection(pid, name, tokens, meta)["id"]


@router.post("/admin/api/codex/import")
async def codex_import(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    try:
        tokens = codex_auth.parse_import(str(data.get("text") or ""))
    except ValueError as e:
        raise HTTPException(400, f"Không đọc được dữ liệu: {e}") from e
    if not tokens.get("access_token") or int(tokens.get("expires_at") or 0) - now_ms() < codex_auth.REFRESH_LEAD_MS:
        if not tokens.get("refresh_token"):
            raise HTTPException(400, "Access token đã hết hạn và không có refresh token")
        try:
            tokens = await codex_auth.refresh_tokens(tokens["refresh_token"])
        except ProviderError as e:
            raise HTTPException(502, e.message) from e
    cid = save_codex_account(st, data.get("provider_id") or "codex", tokens, str(data.get("label") or "") or None)
    return {"ok": True, "connection_id": cid}


# ------------------------------------------------------------------ combos
@router.get("/admin/api/combos")
async def list_combos(request: Request):
    require_admin(request)
    st = S(request)
    out = []
    for c in sorted(st.store.combos.values(), key=lambda c: c["name"].lower()):
        resolved = []
        for spec in c["targets"]:
            try:
                ts, _ = st.router.resolve(spec, 1)
                resolved.append({"spec": spec, "ok": True, "targets": [t.label for t in ts]})
            except GatewayError as e:
                resolved.append({"spec": spec, "ok": False, "error": e.message})
        out.append({**c, "resolved": resolved})
    return {"data": out}


@router.post("/admin/api/combos")
async def save_combo(request: Request):
    require_admin(request)
    try:
        S(request).store.upsert_combo(await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.delete("/admin/api/combos/{name}")
async def delete_combo(name: str, request: Request):
    require_admin(request)
    S(request).store.delete_combo(name)
    return {"ok": True}


# ------------------------------------------------------------------ api keys
@router.get("/admin/api/keys")
async def list_keys(request: Request):
    require_admin(request)
    st = S(request)
    st.usage.flush()
    since = now_ms() - 24 * 3600_000
    stats = {r["key_id"]: dict(r) for r in st.store.q(
        "SELECT key_id, COUNT(*) n, SUM(status>=400) err, SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) tok "
        "FROM usage WHERE ts>=? AND key_id IS NOT NULL GROUP BY key_id", (since,))}
    out = []
    for key in sorted(st.store.keys.values(), key=lambda k: k["created_at"]):
        item = {k: v for k, v in key.items() if k != "key_hash"}
        s = stats.get(key["id"]) or {}
        item["usage_24h"] = {"n": s.get("n") or 0, "err": s.get("err") or 0, "tok": s.get("tok") or 0}
        out.append(item)
    return {"data": out}


@router.post("/admin/api/keys")
async def create_key(request: Request):
    require_admin(request)
    data = await request.json()
    allowed = data.get("allowed_models")
    if isinstance(allowed, str):
        allowed = [m.strip() for m in allowed.split(",") if m.strip()] or None
    raw = S(request).store.create_key(str(data.get("name") or "client"), allowed, int(data.get("rpm") or 0))
    return {"ok": True, "key": raw}


@router.patch("/admin/api/keys/{kid}")
async def update_key(kid: str, request: Request):
    require_admin(request)
    try:
        S(request).store.update_key(kid, await request.json())
    except KeyError as e:
        raise HTTPException(404, "Không có key này") from e
    return {"ok": True}


@router.delete("/admin/api/keys/{kid}")
async def delete_key(kid: str, request: Request):
    require_admin(request)
    S(request).store.delete_key(kid)
    return {"ok": True}


# ------------------------------------------------------------------ settings
@router.get("/admin/api/settings")
async def get_settings(request: Request):
    require_admin(request)
    return {"data": S(request).store.settings}


@router.post("/admin/api/settings")
async def save_settings(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    clean: dict[str, Any] = {}
    for k in ("rtk_enabled", "unknown_model_fallback", "log_errors"):
        if k in data:
            clean[k] = bool(data[k])
    for k, lo, hi in (("usage_retention_days", 1, 90), ("usage_max_rows", 1000, 500000),
                      ("breaker_threshold", 1, 20), ("breaker_cooldown", 5, 3600)):
        if k in data:
            clean[k] = max(lo, min(hi, int(data[k])))
    if "default_model" in data:
        clean["default_model"] = str(data["default_model"] or "").strip()
    st.store.set_settings(clean)
    st.apply_settings()
    return {"ok": True}


# ------------------------------------------------------------------ backup
@router.get("/admin/api/backup")
async def backup(request: Request):
    """Tải bản sao SQLite (token/API key trong đó vẫn được mã hóa bằng SECRET_KEY)."""
    require_admin(request)
    st = S(request)
    st.usage.flush()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        try:
            with st.store.lock:
                st.store.conn.backup(dst)
        finally:
            dst.close()
    except Exception:
        os.remove(tmp)
        raise
    name = time.strftime("unirouter-%Y%m%d-%H%M.db")
    return FileResponse(tmp, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(lambda: os.path.exists(tmp) and os.remove(tmp)))


# ------------------------------------------------------------------ playground
@router.post("/admin/api/playground/image")
async def playground_image(request: Request):
    require_admin(request)
    from .api import finish_images
    st = S(request)
    data = await request.json()
    model = str(data.get("model") or "")
    body = {"prompt": str(data.get("prompt") or "").strip(), "n": 1}
    if not body["prompt"]:
        raise HTTPException(400, "Nhập mô tả ảnh")
    if data.get("size"):
        body["size"] = str(data["size"])
    t0 = time.monotonic()
    try:
        res, target, conn, attempts, _ = await st.router.call("images", model, body)
    except GatewayError as e:
        return {"ok": False, "status": e.status, "error": e.message, "attempts": e.attempts}
    finish_images(request, res, "url")
    ms = int((time.monotonic() - t0) * 1000)
    st.usage.add(key_name="admin", endpoint="images", requested_model=model or "auto", provider=target.provider["id"],
                 model=target.model, connection=conn["label"], status=200, latency_ms=ms, ttfb_ms=ms,
                 output_tokens=len(res.get("data") or []), attempts=len(attempts) + 1)
    return {"ok": True, "target": target.label, "account": conn["label"], "attempts": attempts, "total_ms": ms,
            "images": [{"url": i.get("url"), "revised_prompt": i.get("revised_prompt")}
                       for i in res.get("data") or [] if isinstance(i, dict)]}


@router.post("/admin/api/playground/embed")
async def playground_embed(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    model = str(data.get("model") or "")
    text = str(data.get("input") or "").strip() or "Xin chào"
    t0 = time.monotonic()
    try:
        res, target, conn, attempts, _ = await st.router.call("embeddings", model, {"input": text})
    except GatewayError as e:
        return {"ok": False, "status": e.status, "error": e.message, "attempts": e.attempts}
    vec = ((res.get("data") or [{}])[0] or {}).get("embedding") or []
    return {"ok": True, "target": target.label, "account": conn["label"], "attempts": attempts,
            "total_ms": int((time.monotonic() - t0) * 1000), "dims": len(vec) if isinstance(vec, list) else 0,
            "preview": vec[:8] if isinstance(vec, list) else str(vec)[:80], "usage": res.get("usage")}



@router.post("/admin/api/playground")
async def playground(request: Request):
    require_admin(request)
    st = S(request)
    data = await request.json()
    model = str(data.get("model") or "")
    messages = data.get("messages") or [{"role": "user", "content": str(data.get("message") or "Xin chào")}]
    body = {"model": model, "messages": messages}
    if data.get("system"):
        body["messages"] = [{"role": "system", "content": str(data["system"])}] + list(messages)
    ctx = CallContext(body, False, "playground")
    t0 = time.monotonic()
    try:
        s = await st.router.open(ctx, model)
    except GatewayError as e:
        return {"ok": False, "status": e.status, "error": e.message, "attempts": e.attempts}
    writer = ChatWriter(model)
    try:
        async for ev in s.events():
            writer.feed(ev)
        st.router.done(s, True)
    except Exception as e:  # noqa: BLE001
        st.router.done(s, False, str(e))
        return {"ok": False, "error": getattr(e, "message", str(e)), "target": s.target.label}
    finally:
        await s.aclose()
    res = writer.result()
    msg = res["choices"][0]["message"]
    return {"ok": True, "target": s.target.label, "account": s.connection["label"], "attempts": s.attempts,
            "reply": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or "",
            "tool_calls": msg.get("tool_calls"), "usage": res.get("usage"),
            "ttfb_ms": s.ttfb_ms, "total_ms": int((time.monotonic() - t0) * 1000)}
