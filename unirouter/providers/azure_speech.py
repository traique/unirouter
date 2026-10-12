"""Microsoft Azure AI Speech: chuyển chữ thành giọng nói bằng giọng Neural của Microsoft.

API chính thức, cần key của tài nguyên Speech (gói F0 miễn phí mỗi tháng). Model = tên giọng,
vd vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural hoặc giọng Multilingual đọc được nhiều ngôn ngữ.
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import escape

import httpx

from .. import http
from .base import Provider, ProviderError, Target, classify_http

DEFAULT_REGION = "southeastasia"
VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2,4}(-[A-Za-z]+)?-[A-Za-z0-9]+Neural(HD)?$|^[a-z]{2,3}-[A-Z]{2,4}-[A-Za-z0-9:]+$")
FORMATS = {
    "mp3": ("audio-24khz-48kbitrate-mono-mp3", "audio/mpeg"),
    "opus": ("ogg-24khz-16bit-mono-opus", "audio/ogg"),
    "wav": ("riff-24khz-16bit-mono-pcm", "audio/wav"),
    "pcm": ("raw-24khz-16bit-mono-pcm", "audio/pcm"),
    "aac": ("audio-24khz-48kbitrate-mono-mp3", "audio/mpeg"),  # Azure không có AAC -> trả MP3
    "flac": ("riff-24khz-16bit-mono-pcm", "audio/wav"),
}


def region_of(provider: dict[str, Any], secret: dict[str, Any]) -> str:
    region = (secret.get("region") or (provider.get("options") or {}).get("region") or "").strip().lower()
    if not region:
        m = re.match(r"https?://([a-z0-9]+)\.tts\.speech\.microsoft\.com", provider.get("base_url") or "")
        region = m.group(1) if m else DEFAULT_REGION
    return re.sub(r"[^a-z0-9]", "", region) or DEFAULT_REGION


def tts_base(provider: dict[str, Any], secret: dict[str, Any]) -> str:
    """Base URL dịch vụ TTS: theo region, hoặc base_url tùy chỉnh (Azure Government, proxy nội bộ...)."""
    base = (provider.get("base_url") or "").rstrip("/")
    if base and ".tts.speech.microsoft.com" not in base:
        return base
    return f"https://{region_of(provider, secret)}.tts.speech.microsoft.com"


def is_voice(name: str) -> bool:
    return bool(name) and "Neural" in name and bool(VOICE_RE.match(name))


def build_ssml(text: str, voice: str, speed: float | None) -> str:
    lang = "-".join(voice.split("-")[:2])
    body = escape(text)
    if speed and abs(speed - 1.0) > 0.01:
        pct = max(-50, min(200, round((speed - 1.0) * 100)))
        body = f"<prosody rate='{pct:+d}%'>{body}</prosody>"
    return (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{lang}'>"
            f"<voice name='{escape(voice)}'>{body}</voice></speak>")


class AzureSpeechProvider(Provider):
    type = "azure"

    async def open(self, target, connection, ctx):  # không phải model chat
        raise ProviderError(400, f"{target.provider['name']} chỉ dùng cho chuyển chữ thành giọng nói", "target")

    async def speech(self, target: Target, connection: dict[str, Any], body: dict[str, Any]) -> tuple[bytes, str]:
        provider = target.provider
        secret = connection["_secret"]
        voice = str(body.get("voice") or "")
        if not is_voice(voice):
            voice = target.model if is_voice(target.model) else \
                (provider.get("options") or {}).get("tts_model") or "vi-VN-HoaiMyNeural"
        fmt = str(body.get("response_format") or "mp3").lower()
        out_fmt, ctype = FORMATS.get(fmt, FORMATS["mp3"])
        try:
            speed = float(body.get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        url = tts_base(provider, secret) + "/cognitiveservices/v1"
        headers = {"Ocp-Apim-Subscription-Key": secret.get("api_key") or "", "Content-Type": "application/ssml+xml",
                   "X-Microsoft-OutputFormat": out_fmt, "User-Agent": "UniRouter"}
        ssml = build_ssml(str(body.get("input") or ""), voice, speed)
        try:
            resp = await http.client().post(url, content=ssml.encode("utf-8"), headers=headers, timeout=120)
        except httpx.TimeoutException as e:
            raise ProviderError(504, f"Timeout khi gọi Azure Speech: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(502, f"Lỗi mạng tới Azure Speech: {e}") from e
        if resp.status_code >= 400:
            msg = http.error_message(resp.content, resp.status_code) or resp.reason_phrase
            if resp.status_code == 400:
                raise ProviderError(400, f"Azure Speech từ chối (giọng '{voice}' có thể không tồn tại): {msg}", "target")
            raise classify_http(resp.status_code, f"Azure Speech: {msg}", http.retry_after(resp.headers))
        if not resp.content:
            raise ProviderError(502, "Azure Speech trả về âm thanh rỗng")
        return resp.content, ctype

    async def list_models(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[str]:
        secret = connection["_secret"]
        url = tts_base(provider, secret) + "/cognitiveservices/voices/list"
        resp = await http.client().get(url, headers={"Ocp-Apim-Subscription-Key": secret.get("api_key") or ""},
                                       timeout=30)
        if resp.status_code >= 400:
            raise classify_http(resp.status_code, http.error_message(resp.content, resp.status_code))
        names = [v.get("ShortName") for v in resp.json() if isinstance(v, dict)]
        names = [n for n in names if isinstance(n, str)]
        # Giọng tiếng Việt trước, rồi giọng đa ngôn ngữ (đọc được tiếng Việt), rồi tiếng Anh Mỹ
        vi = sorted(n for n in names if n.startswith("vi-VN"))
        multi = sorted(n for n in names if "Multilingual" in n and n not in vi)
        en = sorted(n for n in names if n.startswith("en-US") and n not in multi)[:10]
        return vi + multi + en
