"""Provider Gemini Web: chat qua gemini.google.com (đăng nhập bằng cookie).

Dùng cùng endpoint StreamGenerate mà web app Gemini dùng. Mỗi request:
  1. ensure_session() -> cookie đã xoay + token trang (at, bl),
  2. dựng payload f.req (định dạng protobuf-like của Bard frontend),
  3. POST, đọc các dòng `wrb.fr`, lấy khối chữ dài nhất làm câu trả lời.

Giới hạn có chủ đích:
  - KHÔNG hỗ trợ tool/function calling (web chỉ giả lập qua prompt, kém tin cậy). Nếu request có
    tools, provider báo scope "target" ngay để router bỏ qua và thử nguồn khác.
  - Stream: Gemini web trả bản tích lũy của câu trả lời; ta phát phần mới thêm so với bản đã phát.
    Nếu model sửa lại đoạn đã phát (hiếm), giữ bản đã phát và không ghép thêm.
  - Tự chữa: request bị từ chối (400/401/403) thì xoay cookie, tải lại token trang và gửi lại một lần.
  - Ảnh đầu vào (image_url data:/http): upload lên máy chủ upload của Gemini web rồi đính kèm vào prompt.
    Mã file được cache theo nội dung ảnh nên ảnh lặp lại trong lịch sử hội thoại chỉ upload một lần.
    Không đọc/upload được ảnh nào thì báo scope "target" để router thử nguồn khác, thay vì trả lời
    như thể không có ảnh.
  - Không embeddings. (Tạo ảnh cố tình bỏ theo yêu cầu.)
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from ..formats.common import message_images, messages_text  # gom messages thành 1 prompt
from . import gemini_web_auth as gw
from .base import CallContext, Event, Provider, ProviderError, Target

STREAM_PATH = ("/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate")
BASE = "https://gemini.google.com"
UPLOAD_URL = "https://content-push.googleapis.com/upload"
UPLOAD_PUSH_ID = "feeds/mcudyrk2a4khkz"  # Push-ID cố định mà web app Gemini dùng khi đính kèm file
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Prompt mặc định khi client chỉ gửi ảnh, không kèm chữ (Gemini web không nhận prompt rỗng).
IMAGE_ONLY_PROMPT = "Describe this image."
UPLOAD_CACHE_MAX = 512
# Cache mã file đã upload theo sha256 nội dung ảnh: bot gửi lại cả lịch sử mỗi lượt nên ảnh cũ không phải
# upload lại. Google giữ file theo hạn ghi trong mã (".../ttl_1d/..."); cache hết hạn sớm hơn để chắc chắn.
_upload_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()  # sha256 -> (mã file, hết hạn epoch)
_uploading: dict[str, asyncio.Task] = {}  # cùng ảnh đang upload dở -> dùng chung một lần upload
_RE_TTL = re.compile(r"/ttl_(\d+)([dh])/")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif",
        "image/heic": "heic", "image/heif": "heif", "image/bmp": "bmp"}

# Dọn nhiễu nội bộ của Gemini khỏi câu trả lời.
_RE_CODE_BLOCK = re.compile(
    r"```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?",
    re.DOTALL)
_RE_CARD = re.compile(r"http://googleusercontent\.com/card_content/\d+\n?")
# Dấu trích dẫn Gemini chèn khi trả lời dựa trên file đính kèm: "[cite_start]", "[cite: 1]", "[cite: 1, 2]".
_RE_CITE = re.compile(r"\[cite_(?:start|end)\]| ?\[cite:\s*\d+(?:\s*,\s*\d+)*\]")
_RE_BARD_ERR = re.compile(r"BardErrorInfo\s*\[(\d+)\]")


# Chế độ model của web app (MODE_CATEGORY trong JS của Gemini): tên ổn định, không gắn số phiên bản
# vì Google nâng model phía sau liên tục. (mode, think)
MODELS: dict[str, tuple[int, int]] = {
    "gemini-flash": (1, 4),
    "gemini-thinking": (2, 0),
    "gemini-pro": (3, 4),
    "gemini-auto": (4, 4),
    "gemini-flash-lite": (6, 4),
}
ALIASES = {"gemini-web": "gemini-flash"}
DEFAULT_MODELS = list(MODELS)


def resolve_mode(model: str) -> tuple[int, int]:
    name = ALIASES.get(model, model)
    if name not in MODELS:
        raise ProviderError(400, f"Gemini Web không có model '{model}'. Dùng: {', '.join(MODELS)}", "request")
    return MODELS[name]


# Gemini web chèn thẻ giao diện riêng (<Section>, <TextBox>, <Elicitations>...) để vẽ thẻ trên web.
# Client API không hiểu các thẻ này -> đổi sang Markdown, bỏ gợi ý câu hỏi tiếp theo.
_RE_ELICIT = re.compile(r"<Elicitations\b[^>]*>.*?</Elicitations>\s*", re.DOTALL)
_RE_TAG = re.compile(r"<(/?)([A-Z][A-Za-z]*)\b([^<>]*?)(/?)>")
_RE_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _rich_tag(m: re.Match) -> str:
    closing, name, attrs = m.group(1), m.group(2), dict(_RE_ATTR.findall(m.group(3)))
    if closing:
        return ""
    if name == "Section":
        return f"### {attrs['title']}" if attrs.get("title") else ""
    if attrs.get("text"):
        sub = f" ({attrs['subtext']})" if attrs.get("subtext") else ""
        return f"**{attrs['text']}**{sub}"
    return attrs.get("title") or attrs.get("label") or ""


def rich_to_markdown(text: str) -> str:
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)  # không đụng vào khối code
    for i in range(0, len(parts), 2):
        seg = _RE_CITE.sub("", _RE_ELICIT.sub("", parts[i]))
        seg = _RE_TAG.sub(_rich_tag, seg)
        parts[i] = re.sub(r"\n{3,}", "\n\n", seg)
    return "".join(parts)


def clean_text(text: str) -> str:
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_CARD.sub("", text)
    return rich_to_markdown(text).strip()


def build_payload(prompt: str, at: str, bl: str, temporary: bool = True,
                  mode: int = 1, think: int = 4,
                  files: list[tuple[str, str]] | None = None) -> tuple[str, dict[str, str]]:
    """files: [(mã upload, tên file)] lấy từ upload_image()."""
    inner: list[Any] = [None] * 102
    attach = [[[fid], name] for fid, name in files] if files else None
    inner[0] = [prompt, 0, None, attach, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    if temporary:  # chat tạm: không đổ vào lịch sử tài khoản
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = mode
    body = {"f.req": json.dumps([None, json.dumps(inner)]), "at": at}
    from urllib.parse import urlencode
    return urlencode(body), {"bl": bl}


def decode_data_url(url: str) -> tuple[str, bytes] | None:
    """data:image/...;base64,... -> (content-type, bytes). None nếu không phải ảnh base64 hợp lệ."""
    head, sep, data = url.partition(",")
    if not sep or not head.startswith("data:") or ";base64" not in head:
        return None
    ctype = head[5:].split(";")[0].strip().lower() or "image/png"
    if not ctype.startswith("image/"):
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    return (ctype, raw) if raw else None


async def load_image(url: str) -> tuple[str, bytes] | None:
    if url.startswith("data:"):
        got = decode_data_url(url)
    elif url.startswith(("http://", "https://")):
        from .codex import download_image
        got = await download_image(url, MAX_IMAGE_BYTES)
    else:
        return None
    if not got or len(got[1]) > MAX_IMAGE_BYTES:
        return None
    return got


def upload_ttl(file_id: str) -> float:
    """Số giây được dùng lại mã file: 80% hạn Google ghi trong mã, không rõ hạn thì 1 giờ."""
    m = _RE_TTL.search(file_id)
    if not m:
        return 3600.0
    return int(m.group(1)) * (86400 if m.group(2) == "d" else 3600) * 0.8


def _extract_line(line: str) -> str | None:
    """Chữ trả lời trong một dòng wrb.fr (None nếu dòng không phải khối trả lời)."""
    if '"wrb.fr"' not in line:
        return None
    try:
        arr = json.loads(line)
        payload = json.loads(arr[0][2])
    except (ValueError, IndexError, TypeError):
        return None
    if not (isinstance(payload, list) and len(payload) > 4 and payload[4]):
        return None
    best = ""
    for part in payload[4]:
        if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
            for t in part[1]:
                if isinstance(t, str) and len(t) > len(best):
                    best = t
    return best or None


def _extract_text(raw: str) -> str:
    """Lấy khối chữ dài nhất từ các dòng wrb.fr của phản hồi StreamGenerate."""
    best = ""
    for line in raw.split("\n"):
        t = _extract_line(line)
        if t and len(t) > len(best):
            best = t
    return best


def _safe_cut(text: str) -> str:
    """Cắt bỏ thẻ <...> hoặc [cite...] đang viết dở ở cuối, để phần phát ra khi stream không đổi khi
    thẻ đóng lại (và bị dọn đi)."""
    for open_, close in (("<", ">"), ("[", "]")):
        i = text.rfind(open_)
        if i >= 0 and close not in text[i:]:
            text = text[:i]
    return text


class GeminiWebProvider(Provider):
    type = "gemini_web"

    def __init__(self, store: Any, proxy: str | None = None):
        self.store = store
        self.proxy = proxy

    def _tools_requested(self, ctx: CallContext) -> bool:
        body = ctx.body or {}
        if body.get("tools") and body.get("tool_choice") != "none":
            return True
        # /v1/responses passthrough cũng có thể mang tools
        rb = ctx.responses_body or {}
        return bool(rb.get("tools") and rb.get("tool_choice") != "none")

    async def open(self, target: Target, connection: dict[str, Any], ctx: CallContext) -> AsyncIterator[Event]:
        if self._tools_requested(ctx):
            # Web không làm tool đáng tin -> để router thử provider khác.
            raise ProviderError(400, "Gemini Web không hỗ trợ tool calling", "target")
        mode, think = resolve_mode(target.model)
        messages = ctx.body.get("messages") or []
        prompt = messages_text(messages)
        images = message_images(messages)
        if not prompt.strip():
            if not images:
                raise ProviderError(400, "Prompt rỗng", "request")
            prompt = IMAGE_ONLY_PROMPT
        if len(images) > MAX_IMAGES:
            raise ProviderError(400, f"Gemini Web nhận tối đa {MAX_IMAGES} ảnh mỗi request", "target")
        temporary = (target.provider.get("options") or {}).get("temporary_chats", True)
        files = await self._upload_images(images) if images else None
        return self._run(connection, prompt, bool(temporary), mode, think, ctx.stream, files)

    async def _upload_images(self, urls: list[str]) -> list[tuple[str, str]]:
        """Upload mọi ảnh; chỉ cần một ảnh lỗi là báo scope "target" (router thử nguồn khác)."""
        loaded = await asyncio.gather(*(load_image(u) for u in urls))
        for i, got in enumerate(loaded):
            if not got:
                raise ProviderError(400, f"Gemini Web không đọc được ảnh #{i + 1} (cần data: base64 hoặc URL "
                                         f"công khai, tối đa {MAX_IMAGE_BYTES // (1024 * 1024)}MB)", "target")
        names = [f"image_{i + 1}.{_EXT.get(ct, 'png')}" for i, (ct, _) in enumerate(loaded)]
        ids = await asyncio.gather(*(self._upload_cached(ct, data, name) for (ct, data), name in zip(loaded, names)))
        return list(zip(ids, names))

    async def _upload_cached(self, ctype: str, data: bytes, name: str) -> str:
        key = hashlib.sha256(data).hexdigest()
        hit = _upload_cache.get(key)
        if hit and hit[1] > time.time():
            _upload_cache.move_to_end(key)
            return hit[0]
        task = _uploading.get(key)
        if task is None:
            task = asyncio.ensure_future(self._upload(ctype, data, name))
            _uploading[key] = task
            # đánh dấu đã đọc lỗi để không bị log "exception was never retrieved" khi mọi request chờ đã hủy
            task.add_done_callback(lambda t: (_uploading.pop(key, None), t.cancelled() or t.exception()))
        file_id = await asyncio.shield(task)  # một request bị hủy không hủy upload mà request khác đang chờ
        _upload_cache[key] = (file_id, time.time() + upload_ttl(file_id))
        _upload_cache.move_to_end(key)
        while len(_upload_cache) > UPLOAD_CACHE_MAX:
            _upload_cache.popitem(last=False)
        return file_id

    async def _upload(self, ctype: str, data: bytes, name: str) -> str:
        from curl_cffi import CurlMime

        mime = CurlMime()
        mime.addpart(name="file", content_type=ctype, filename=name, data=data)
        headers = {"User-Agent": gw.UA, "Push-ID": UPLOAD_PUSH_ID, "Origin": BASE, "Referer": BASE + "/"}
        kw = gw._kw(UPLOAD_URL, self.proxy, headers=headers, multipart=mime, timeout=60, allow_redirects=True)
        try:
            resp = await gw.session().post(UPLOAD_URL, **kw)
        except Exception as e:  # noqa: BLE001 - curl_cffi.CurlError
            raise ProviderError(502, f"Lỗi mạng khi upload ảnh lên Gemini Web: {e}", "target") from e
        finally:
            mime.close()
        file_id = resp.text.strip()
        if resp.status_code >= 400 or not file_id or "\n" in file_id:
            raise ProviderError(502, f"Gemini Web từ chối upload ảnh (HTTP {resp.status_code})", "target")
        return file_id

    async def _post(self, connection: dict[str, Any], prompt: str, temporary: bool, mode: int, think: int,
                    files: list[tuple[str, str]] | None = None) -> Any:
        sess = await gw.ensure_session(self.store, connection, self.proxy)
        data, params = build_payload(prompt, sess["at"], sess["bl"], temporary, mode, think, files)
        reqid = int(time.time()) % 1000000
        url = f"{BASE}{STREAM_PATH}?bl={params['bl']}&hl=en&_reqid={reqid}&rt=c"
        headers = {
            "User-Agent": gw.UA,
            "Cookie": gw.cookie_header(sess["cookies"]),
            "Origin": BASE,
            "Referer": BASE + "/app",
            "X-Same-Domain": "1",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        }
        auth = gw.sapisid_hash(sess["cookies"])
        if auth:
            headers["Authorization"] = auth
        kw = gw._kw(url, self.proxy, headers=headers, data=data.encode(), timeout=180, stream=True)
        try:
            resp = await gw.session().post(url, **kw)
        except Exception as e:  # noqa: BLE001 - curl_cffi.CurlError
            raise ProviderError(502, f"Lỗi mạng tới Gemini Web: {e}", "target") from e
        if resp.status_code in (400, 401, 403):
            # Token trang (at/bl) cũ hoặc phiên chết: người gọi quyết định thử lại sau khi tải lại trang.
            await resp.aclose()
            gw.invalidate(connection["id"])
            raise ProviderError(resp.status_code, f"Gemini Web từ chối (HTTP {resp.status_code})", "account", 300)
        if resp.status_code >= 400:
            await resp.aclose()
            raise ProviderError(resp.status_code, f"Gemini Web HTTP {resp.status_code}", "target")
        return resp

    async def _run(self, connection: dict[str, Any], prompt: str, temporary: bool,
                   mode: int, think: int, stream: bool,
                   files: list[tuple[str, str]] | None = None) -> AsyncIterator[Event]:
        try:
            resp = await self._post(connection, prompt, temporary, mode, think, files)
        except ProviderError as e:
            if e.scope != "account":
                raise
            # Tự chữa một lần: token trang/cookie có thể vừa đổi -> xoay cookie, tải lại trang, gửi lại.
            await gw.ensure_session(self.store, connection, self.proxy, force_rotate=True, refresh_page=True)
            resp = await self._post(connection, prompt, temporary, mode, think, files)

        emitted = ""
        final = ""
        buf = b""
        try:
            async for chunk in resp.aiter_content():
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw_line in lines:
                    line = raw_line.decode("utf-8", "replace")
                    if "BardErrorInfo" in line:
                        err = _RE_BARD_ERR.search(line)
                        raise ProviderError(502, f"Gemini Web từ chối: BardErrorInfo [{err.group(1) if err else '?'}]",
                                            "target")
                    t = _extract_line(line)
                    if not t:
                        continue
                    final = t
                    if not stream:
                        continue
                    cleaned = clean_text(_safe_cut(t))
                    if len(cleaned) > len(emitted) and cleaned.startswith(emitted):
                        yield {"type": "text", "text": cleaned[len(emitted):]}
                        emitted = cleaned
            tail = buf.decode("utf-8", "replace")
            if "BardErrorInfo" in tail:
                raise ProviderError(502, "Gemini Web từ chối: BardErrorInfo", "target")
            t = _extract_line(tail)
            if t:
                final = t
        finally:
            await resp.aclose()

        text = clean_text(final)
        if not text:
            if emitted:
                text = emitted
            else:
                raise ProviderError(502, "Gemini Web trả về rỗng (giao diện có thể đã đổi)", "target")
        if text.startswith(emitted):
            if len(text) > len(emitted):
                yield {"type": "text", "text": text[len(emitted):]}
        elif not emitted:
            yield {"type": "text", "text": text}
        # (đã phát một phần mà bản cuối lại khác hẳn: giữ bản đã phát, không ghép hai bản)
        gw.mark_alive(self.store, connection["id"])
        yield {"type": "usage", "input": len(prompt) // 4, "output": len(text) // 4, "cached": 0}
        yield {"type": "finish", "reason": "stop"}

    async def list_models_detailed(self, provider: dict[str, Any],
                                   connection: dict[str, Any]) -> list[dict[str, Any]]:
        # Gemini web không có API liệt kê model; dùng danh sách tĩnh của provider.
        return [{"id": m, "free": True} for m in (provider.get("models") or DEFAULT_MODELS)]
