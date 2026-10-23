"""Đăng nhập ChatGPT (Codex OAuth): device code, nhập auth.json, làm mới token.

Luồng giống Codex CLI chính thức (client_id app_EMoamEEZ73f0CkXaXp7hrann): đăng nhập bằng mã
thiết bị, làm mới token trước khi hết hạn và xử lý refresh token xoay vòng.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from curl_cffi.requests import AsyncSession

from .base import ProviderError

AUTH_BASE = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
PKCE_REDIRECT = "http://localhost:1455/auth/callback"
PKCE_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
# Scope mà token Codex nên có; thiếu thì chỉ cảnh báo trong trang quản trị (không chặn).
CODEX_SCOPES = ("api.connectors.read", "api.connectors.invoke")
CODEX_VERSION = "0.159.0"
USER_AGENT = f"codex_cli_rs/{CODEX_VERSION}"
VERIFICATION_URL = "https://auth.openai.com/codex/device"
DEVICE_REDIRECT = "https://auth.openai.com/deviceauth/callback"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Access token sống ~1h. Chỉ refresh khi còn <10 phút: refresh quá sớm/quá thường
# làm xoay refresh token liên tục, dùng lại token cũ sẽ bị OpenAI thu hồi cả phiên.
REFRESH_LEAD_MS = 10 * 60 * 1000
IMPERSONATE = "chrome"

_session: AsyncSession | None = None
_locks: dict[str, asyncio.Lock] = {}


def session() -> AsyncSession:
    global _session
    if _session is None:
        _session = AsyncSession(impersonate=IMPERSONATE, timeout=300)
    return _session


async def close() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


async def abort_response(resp: Any) -> None:
    """Đóng stream curl_cffi NGAY (aclose() mặc định đợi tải hết body -> tốn quota khi client đã ngắt)."""
    try:
        quit_now = getattr(resp, "quit_now", None)
        if quit_now is not None:
            quit_now.set()
        task = getattr(resp, "astream_task", None)
        if task is not None and not task.done():
            curl = getattr(resp, "curl", None)
            if curl is not None and _session is not None and getattr(_session, "acurl", None) is not None:
                try:
                    _session.acurl.remove_handle(curl)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except BaseException:
                task.cancel()
    except Exception:
        pass


def hv(url: str) -> dict[str, Any]:
    """HTTP thường (chỉ dùng khi test local) không nâng cấp h2c được -> ép HTTP/1.1."""
    return {"http_version": "v1"} if url.startswith("http://") else {}


def decode_jwt(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def account_info(tokens: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for key in ("id_token", "access_token"):
        claims = decode_jwt(tokens.get(key) or "")
        if not claims:
            continue
        auth = claims.get("https://api.openai.com/auth") or {}
        profile = claims.get("https://api.openai.com/profile") or {}
        info.setdefault("account_id", auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id"))
        info.setdefault("plan", auth.get("chatgpt_plan_type"))
        info.setdefault("email", claims.get("email") or profile.get("email"))
        if key == "access_token" and claims.get("exp"):
            info["exp_ms"] = int(claims["exp"]) * 1000
        if key == "access_token" and (claims.get("scp") or claims.get("scope")):
            scopes = claims.get("scp") or claims.get("scope")
            scopes = scopes.split() if isinstance(scopes, str) else list(scopes)
            info["missing_scopes"] = [s for s in CODEX_SCOPES if s not in scopes]
    return {k: v for k, v in info.items() if v}


def _auth_headers(json_body: bool = True) -> dict[str, str]:
    h = {"user-agent": USER_AGENT, "originator": "codex_cli_rs", "accept": "application/json"}
    h["content-type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
    return h


def _err(resp: Any) -> str:
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)
        return str(data.get("error_description") or err or data)[:300]
    except Exception:
        if "text/html" in (resp.headers.get("content-type") or ""):
            return f"HTTP {resp.status_code} (trang HTML - có thể bị Cloudflare chặn IP máy chủ)"
        return f"HTTP {resp.status_code}: {resp.text[:200]}"


# ------------------------------------------------------------------ device code
async def device_start() -> dict[str, Any]:
    resp = await session().post(f"{AUTH_BASE}/api/accounts/deviceauth/usercode", headers=_auth_headers(),
                                json={"client_id": CLIENT_ID}, timeout=30, **hv(AUTH_BASE))
    if resp.status_code >= 400:
        raise ProviderError(502, f"Không khởi tạo được đăng nhập: {_err(resp)}", "request")
    data = resp.json()
    device_auth_id = data.get("device_auth_id")
    user_code = data.get("user_code") or data.get("usercode")
    if not device_auth_id or not user_code:
        raise ProviderError(502, "Phản hồi đăng nhập không hợp lệ", "request")
    try:
        interval = max(int(data.get("interval", 5)), 3)
    except (TypeError, ValueError):
        interval = 5
    return {"device_auth_id": device_auth_id, "user_code": user_code, "interval": interval,
            "verification_url": VERIFICATION_URL}


async def device_poll(device_auth_id: str, user_code: str) -> dict[str, Any] | None:
    """None = người dùng chưa xác nhận. Trả về tokens khi xong."""
    resp = await session().post(f"{AUTH_BASE}/api/accounts/deviceauth/token", headers=_auth_headers(),
                                json={"device_auth_id": device_auth_id, "user_code": user_code}, timeout=30,
                                **hv(AUTH_BASE))
    if resp.status_code in (403, 404):
        try:
            data = resp.json()
        except Exception:
            data = {}
        err = data.get("error") if isinstance(data, dict) else None
        code = (err.get("code") if isinstance(err, dict) else err) or ""
        if not code or "pending" in str(code).lower():
            return None
        raise ProviderError(502, f"Đăng nhập thất bại: {_err(resp)}", "request")
    if resp.status_code >= 400:
        raise ProviderError(502, f"Đăng nhập thất bại: {_err(resp)}", "request")
    data = resp.json()
    code, verifier = data.get("authorization_code"), data.get("code_verifier")
    if not code or not verifier:
        raise ProviderError(502, "Thiếu authorization_code/code_verifier", "request")
    tok = await session().post(f"{AUTH_BASE}/oauth/token", headers=_auth_headers(False), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": DEVICE_REDIRECT,
        "client_id": CLIENT_ID, "code_verifier": verifier}, timeout=30, **hv(AUTH_BASE))
    if tok.status_code >= 400:
        raise ProviderError(502, f"Đổi token thất bại: {_err(tok)}", "request")
    return normalize_tokens(tok.json())


# ------------------------------------------------------------------ PKCE (dán callback)
def pkce_challenge() -> tuple[str, str]:
    """Trả về (verifier, challenge) cho luồng PKCE S256."""
    import hashlib

    verifier = base64.urlsafe_b64encode(__import__("secrets").token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def pkce_authorize_url(state: str, challenge: str) -> str:
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": PKCE_REDIRECT,
        "scope": PKCE_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def parse_callback(callback_url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(callback_url.strip()).query)
    return {k: v[0] for k, v in q.items() if v}


async def pkce_exchange(code: str, verifier: str) -> dict[str, Any]:
    tok = await session().post(f"{AUTH_BASE}/oauth/token", headers=_auth_headers(False), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": PKCE_REDIRECT,
        "client_id": CLIENT_ID, "code_verifier": verifier}, timeout=30, **hv(AUTH_BASE))
    if tok.status_code >= 400:
        raise ProviderError(502, f"Đổi token thất bại: {_err(tok)}", "request")
    return normalize_tokens(tok.json())


def normalize_tokens(raw: dict[str, Any]) -> dict[str, Any]:
    access = raw.get("access_token")
    if not access:
        raise ProviderError(502, "Phản hồi không có access_token", "request")
    tokens = {"access_token": access, "refresh_token": raw.get("refresh_token"), "id_token": raw.get("id_token")}
    info = account_info(tokens)
    expires_in = raw.get("expires_in")
    tokens["expires_at"] = info.get("exp_ms") or (int(time.time() * 1000) + int(expires_in or 3600) * 1000)
    tokens["refreshed_at"] = int(time.time() * 1000)
    return tokens


async def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    resp = await session().post(f"{AUTH_BASE}/oauth/token", headers=_auth_headers(False), data={
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID,
        "scope": "openid profile email offline_access"}, timeout=30, **hv(AUTH_BASE))
    if resp.status_code >= 400:
        msg = _err(resp)
        low = msg.lower()
        fatal = any(k in low for k in ("invalid_grant", "reused", "revoked", "expired", "invalid_request"))
        raise ProviderError(401, f"Làm mới token ChatGPT thất bại: {msg}", "account", 10**7 if fatal else 120)
    data = resp.json()
    if not data.get("refresh_token"):
        data["refresh_token"] = refresh_token
    return normalize_tokens(data)


def parse_import(text: str) -> dict[str, Any]:
    """Nhận auth.json của Codex CLI (~/.codex/auth.json), JSON token, hoặc refresh token thuần."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Nội dung trống")
    if text.startswith("{"):
        data = json.loads(text)
        tok = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
        out = {"access_token": tok.get("access_token") or tok.get("accessToken"),
               "refresh_token": tok.get("refresh_token") or tok.get("refreshToken"),
               "id_token": tok.get("id_token") or tok.get("idToken")}
        if not out["refresh_token"] and not out["access_token"]:
            raise ValueError("Không tìm thấy access_token/refresh_token trong JSON")
        if out["access_token"]:
            info = account_info(out)
            out["expires_at"] = info.get("exp_ms") or int(time.time() * 1000) + 3600_000
        else:
            out["expires_at"] = 0
        if tok.get("account_id"):
            out["account_id"] = tok["account_id"]
        return out
    if text.count(".") == 2 and len(text) > 200:
        info = account_info({"access_token": text})
        return {"access_token": text, "refresh_token": None, "id_token": None, "expires_at": info.get("exp_ms", 0)}
    return {"access_token": None, "refresh_token": text, "id_token": None, "expires_at": 0}


# ------------------------------------------------------------------ token cache
async def ensure_access_token(store: Any, connection: dict[str, Any], force: bool = False) -> tuple[str, str]:
    """Trả về (access_token, account_id), tự refresh khi gần hết hạn."""
    cid = connection["id"]
    lock = _locks.setdefault(cid, asyncio.Lock())
    async with lock:
        secret = store.connection_secret(cid)
        meta = dict(store.connections[cid]["meta"]) if cid in store.connections else dict(connection.get("meta") or {})
        now = int(time.time() * 1000)
        if not force and secret.get("access_token") and int(secret.get("expires_at") or 0) - now > REFRESH_LEAD_MS:
            return secret["access_token"], meta.get("account_id") or ""
        if not secret.get("refresh_token"):
            if secret.get("access_token") and int(secret.get("expires_at") or 0) > now and not force:
                return secret["access_token"], meta.get("account_id") or ""
            raise ProviderError(401, "Token ChatGPT đã hết hạn và không có refresh token. Hãy đăng nhập lại.",
                                "account", 10**7)
        new = await refresh_tokens(secret["refresh_token"])
        info = account_info(new)
        meta.update({k: v for k, v in info.items() if k in ("account_id", "email", "plan")})
        meta["missing_scopes"] = info.get("missing_scopes") or []
        try:
            store.update_connection_secret(cid, new, meta)
            store.set_connection_error(cid, None)
        except Exception as exc:  # noqa: BLE001 - vd đĩa đầy: ĐỪNG vứt token đã xoay
            # Token mới (đặc biệt refresh_token đã xoay) phải được dùng, nếu không lần sau sẽ gửi lại
            # refresh_token cũ và OpenAI thu hồi cả phiên. Giữ trong RAM để request này và các request
            # kế tiếp dùng được; keepalive/ lần refresh sau sẽ thử lưu lại.
            import logging
            logging.getLogger("unirouter.codex").error(
                "Không lưu được token ChatGPT đã làm mới (%s); giữ tạm trong bộ nhớ.", exc)
            c = store.connections.get(cid)
            if c is not None:
                try:
                    c["secret_enc"] = store.crypto.enc(new)
                    c["meta"] = meta
                except Exception:  # noqa: BLE001
                    pass
        return new["access_token"], meta.get("account_id") or ""


async def fetch_usage(store: Any, connection: dict[str, Any]) -> dict[str, Any]:
    access, account_id = await ensure_access_token(store, connection)
    headers = {"Authorization": f"Bearer {access}", "originator": "codex_cli_rs", "User-Agent": USER_AGENT,
               "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    resp = await session().get(USAGE_URL, headers=headers, timeout=30, **hv(USAGE_URL))
    if resp.status_code >= 400:
        raise ProviderError(resp.status_code, _err(resp), "request")
    return resp.json()
