"""Phiên Gemini Web (đăng nhập bằng cookie trình duyệt).

Khác với Codex (OAuth có refresh token), gemini.google.com chỉ là phiên trình duyệt:
không có refresh token, nên cách giữ phiên sống là XOAY cookie định kỳ như Chrome tự làm.
Google liên tục cấp lại `__Secure-1PSIDTS` qua endpoint RotateCookies; nếu server giữ bản cũ
thì phiên chết sau vài giờ. Module này:

  - đọc/ghi cookie trong secret của connection (đã mã hóa bằng SECRET_KEY, như mọi secret khác),
  - gọi RotateCookies rồi LƯU cookie mới vào DB (quan trọng: không lưu thì restart là mất),
  - lấy token trang (`SNlM0e` = xsrf, `cfb2h` = build id) cần cho mỗi request,
  - phát hiện phiên chết / bị bắt xác minh để router chuyển nguồn và admin báo đỏ.

Chỉ dùng thư viện chuẩn + curl_cffi (đã có sẵn cho Codex) để giả lập fingerprint trình duyệt.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from curl_cffi.requests import AsyncSession

from .base import ProviderError

APP_URL = "https://gemini.google.com/app"
ROTATE_URL = "https://accounts.google.com/RotateCookies"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
IMPERSONATE = "chrome"
# Xoay cookie khi bản hiện tại đã quá ~9 phút (Chrome xoay ~mỗi 10 phút).
ROTATE_AFTER_MS = 9 * 60 * 1000
# Token trang (at, bl) ít đổi: giữ trong bộ nhớ 1 giờ để không tải lại trang /app (~1 MB) cho mỗi request.
PAGE_TTL_MS = 60 * 60 * 1000
# Body cố định mà trang accounts.google.com gửi khi tự xoay cookie (giống thư viện gemini-webapi).
ROTATE_BODY = b'[000,"-0000000000000000000"]'
# Cookie bắt buộc để coi là "có đăng nhập".
REQUIRED_COOKIE = "__Secure-1PSID"
# Dấu hiệu Google chặn/bắt xác minh thay vì trả trang app.
SORRY_MARKERS = ("/sorry/", "unusual traffic", "detected unusual")

_session: AsyncSession | None = None
_locks: dict[str, asyncio.Lock] = {}
_pages: dict[str, dict[str, Any]] = {}  # connection id -> {"at","bl","ts"}


def session() -> AsyncSession:
    """Một phiên HTTP dùng chung. Mọi request đều gửi cookie tường minh và KHÔNG lưu cookie trả về vào
    jar của phiên (discard_cookies), để nhiều tài khoản Gemini không lẫn cookie của nhau."""
    global _session
    if _session is None:
        _session = AsyncSession(impersonate=IMPERSONATE, timeout=60)
    return _session


def _kw(url: str, proxy: str | None, **extra: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {"discard_cookies": True, **hv(url), **extra}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    return kw


def hv(url: str) -> dict[str, Any]:
    """HTTP thường (chỉ khi test local với mock) cần ép HTTP/1.1; gemini.google.com (https) để mặc định."""
    return {"http_version": "v1"} if url.startswith("http://") else {}


async def close() -> None:
    global _session
    _pages.clear()
    if _session is not None:
        await _session.close()
        _session = None


# ------------------------------------------------------------------ cookie parse
def parse_cookie_text(text: str) -> dict[str, Any]:
    """Nhận cookie dạng 1 dòng (SID=..; __Secure-1PSID=..), JSON, hoặc Netscape.

    Trả về {"cookies": {name: value}}. Lỗi nếu thiếu cookie đăng nhập cốt lõi.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Nội dung cookie trống")
    cookies: dict[str, str] = {}
    if text.startswith("{"):
        data = json.loads(text)
        raw = data.get("cookie") or data.get("cookies") or data
        if isinstance(raw, str):
            cookies = _split_cookie_line(raw)
        elif isinstance(raw, dict):
            cookies = {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int))}
        elif isinstance(raw, list):  # danh sách {name,value} kiểu extension xuất ra
            for it in raw:
                if isinstance(it, dict) and it.get("name"):
                    cookies[str(it["name"])] = str(it.get("value", ""))
    elif "\t" in text and "\n" in text:  # Netscape cookies.txt
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    else:
        cookies = _split_cookie_line(text)
    cookies = {k: v for k, v in cookies.items() if k and v}
    if REQUIRED_COOKIE not in cookies:
        raise ValueError(f"Thiếu cookie {REQUIRED_COOKIE}. Hãy xuất cookie của gemini.google.com khi đã đăng nhập.")
    return {"cookies": cookies}


def _split_cookie_line(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in line.replace("\n", ";").split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def account_hint(cookies: dict[str, str]) -> dict[str, Any]:
    """Nhãn nhận dạng không nhạy cảm (để phân biệt tài khoản, không lộ cookie)."""
    sid = cookies.get(REQUIRED_COOKIE, "")
    return {"cookie_fp": sid[:6] + "…" + sid[-4:] if len(sid) > 12 else "set"}


# ------------------------------------------------------------------ page tokens
_TOKEN_RE = {
    "at": re.compile(r'"SNlM0e":"([^"]+)"'),
    "bl": re.compile(r'"cfb2h":"([^"]+)"'),
}


async def fetch_page(cookies: dict[str, str], proxy: str | None = None) -> dict[str, Any]:
    """Tải trang /app, trả về {"at","bl","ok"} và phát hiện phiên chết."""
    headers = {"User-Agent": UA, "Cookie": cookie_header(cookies)}
    try:
        resp = await session().get(APP_URL, **_kw(APP_URL, proxy, headers=headers, timeout=30, allow_redirects=True))
    except Exception as e:  # curl_cffi.CurlError
        raise ProviderError(502, f"Không kết nối được gemini.google.com: {e}", "target") from e
    body = resp.text or ""
    final_url = str(getattr(resp, "url", "") or "")
    low = (final_url + " " + body[:4000]).lower()
    if any(m in low for m in SORRY_MARKERS):
        raise ProviderError(401, "Google bắt xác minh lại (/sorry/). Cookie có thể chạy từ IP lạ hoặc đã hết hạn — "
                                 "đăng nhập lại và dán cookie mới.", "account", 600)
    at = _TOKEN_RE["at"].search(body)
    bl = _TOKEN_RE["bl"].search(body)
    if not at or not bl:
        # Trang không có token = chưa đăng nhập được (cookie hết hạn).
        raise ProviderError(401, "Cookie Gemini đã hết hạn hoặc không đăng nhập được. Dán cookie mới.", "account", 300)
    return {"at": at.group(1), "bl": bl.group(1), "ok": True}


# ------------------------------------------------------------------ rotate cookie
def _merge_set_cookie(cookies: dict[str, str], resp: Any) -> bool:
    """Ghép cookie mới từ các header Set-Cookie của response vào dict hiện có. True nếu có đổi."""
    changed = False
    try:
        lines = resp.headers.get_list("set-cookie")
    except Exception:  # noqa: BLE001
        lines = []
    for line in lines:
        pair = line.split(";", 1)[0]
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value and cookies.get(name) != value:
            cookies[name] = value
            changed = True
    return changed


async def rotate_cookies(cookies: dict[str, str], proxy: str | None = None) -> bool | None:
    """Gọi RotateCookies để lấy __Secure-1PSIDTS mới.

    True = có cookie mới, False = Google trả lời nhưng không đổi gì, None = lỗi mạng/HTTP.
    Lỗi ở đây KHÔNG coi là phiên chết: cookie cũ vẫn có thể còn dùng được.
    """
    headers = {
        "User-Agent": UA,
        "Cookie": cookie_header(cookies),
        "Content-Type": "application/json",
        "Origin": "https://accounts.google.com",
    }
    try:
        resp = await session().post(ROTATE_URL, **_kw(ROTATE_URL, proxy, headers=headers, data=ROTATE_BODY, timeout=30))
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code >= 400:
        return None
    return _merge_set_cookie(cookies, resp)


# ------------------------------------------------------------------ session cache
async def _rotate_locked(store: Any, cid: str, secret: dict[str, Any], cookies: dict[str, str],
                         proxy: str | None) -> dict[str, Any]:
    """Xoay cookie rồi lưu (đang giữ khóa). Lỗi mạng thì giữ nguyên mốc cũ để lần sau thử lại."""
    result = await rotate_cookies(cookies, proxy)
    if result is None:
        return secret
    secret = {**secret, "cookies": cookies, "rotated_at": int(time.time() * 1000)}
    store.update_connection_secret(cid, secret)
    return secret


def invalidate(cid: str) -> None:
    """Bỏ token trang đã cache (gọi khi Gemini từ chối request với token đó)."""
    _pages.pop(cid, None)


async def ensure_session(store: Any, connection: dict[str, Any], proxy: str | None = None,
                         force_rotate: bool = False, refresh_page: bool = False) -> dict[str, Any]:
    """Trả về {"cookies","at","bl"} sẵn sàng gọi API; tự xoay cookie và lưu lại khi cần.

    Token trang được cache PAGE_TTL_MS; refresh_page=True ép tải lại (dùng khi request bị từ chối hoặc
    keepalive muốn kiểm tra phiên). Khóa theo connection để request đồng thời không xoay chồng nhau.
    """
    cid = connection["id"]
    lock = _locks.setdefault(cid, asyncio.Lock())
    async with lock:
        secret = store.connection_secret(cid)
        cookies = dict(secret.get("cookies") or {})
        if REQUIRED_COOKIE not in cookies:
            raise ProviderError(401, "Chưa có cookie Gemini. Dán cookie trong trang quản trị.", "account", 10 ** 7)
        now = int(time.time() * 1000)
        if force_rotate or now - int(secret.get("rotated_at") or 0) > ROTATE_AFTER_MS:
            await _rotate_locked(store, cid, secret, cookies, proxy)
        page = _pages.get(cid)
        if refresh_page or not page or now - page["ts"] > PAGE_TTL_MS:
            try:
                page = {**await fetch_page(cookies, proxy), "ts": now}
            except ProviderError as e:
                if e.scope == "account":
                    _pages.pop(cid, None)
                raise
            _pages[cid] = page
        store.set_connection_error(cid, None)
        return {"cookies": cookies, "at": page["at"], "bl": page["bl"]}


async def rotate_and_save(store: Any, connection: dict[str, Any], proxy: str | None = None) -> bool:
    """Chỉ xoay cookie + lưu (không tải trang). Dùng cho keepalive mỗi ~10 phút, rất nhẹ."""
    cid = connection["id"]
    lock = _locks.setdefault(cid, asyncio.Lock())
    async with lock:
        secret = store.connection_secret(cid)
        cookies = dict(secret.get("cookies") or {})
        if REQUIRED_COOKIE not in cookies:
            return False
        before = dict(cookies)
        await _rotate_locked(store, cid, secret, cookies, proxy)
        return cookies != before


def sapisid_hash(cookies: dict[str, str], origin: str = "https://gemini.google.com") -> str | None:
    """Header Authorization SAPISIDHASH như trình duyệt gửi kèm request tới Google."""
    import hashlib

    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        return None
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"
