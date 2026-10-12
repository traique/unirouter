"""Giọng đọc Microsoft Edge (thư viện edge-tts) - không cần API key.

Dùng dịch vụ "Đọc to" của trình duyệt Edge, cùng bộ giọng Neural với Azure
(vi-VN-HoaiMyNeural, vi-VN-NamMinhNeural...). Đây KHÔNG phải API chính thức:
Microsoft có thể chặn hoặc thay đổi bất cứ lúc nào, nên nên kèm Azure Speech để dự phòng.
"""
from __future__ import annotations

import importlib
from typing import Any

from .azure_speech import is_voice
from .base import Provider, ProviderError, Target

DEFAULT_VOICE = "vi-VN-HoaiMyNeural"


def _lib():
    try:
        return importlib.import_module("edge_tts")
    except ImportError as e:
        raise ProviderError(501, "Chưa cài thư viện edge-tts (pip install edge-tts)", "target") from e


def rate_of(speed: Any) -> str:
    try:
        s = float(speed or 1.0)
    except (TypeError, ValueError):
        s = 1.0
    return f"{max(-50, min(200, round((s - 1.0) * 100))):+d}%"


class EdgeSpeechProvider(Provider):
    type = "edge"

    async def open(self, target, connection, ctx):
        raise ProviderError(400, f"{target.provider['name']} chỉ dùng cho chuyển chữ thành giọng nói", "target")

    async def speech(self, target: Target, connection: dict[str, Any], body: dict[str, Any]) -> tuple[bytes, str]:
        lib = _lib()
        voice = str(body.get("voice") or "")
        if not is_voice(voice):
            voice = target.model if is_voice(target.model) else \
                (target.provider.get("options") or {}).get("tts_model") or DEFAULT_VOICE
        text = str(body.get("input") or "")
        audio = bytearray()
        try:
            comm = lib.Communicate(text, voice, rate=rate_of(body.get("speed")))
            async for chunk in comm.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    audio.extend(chunk["data"])
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 - lỗi websocket / bị chặn / giọng sai
            name = type(e).__name__
            msg = str(e)[:300]
            if name == "NoAudioReceived":
                raise ProviderError(400, f"Edge TTS không trả âm thanh (giọng '{voice}' có thể không tồn tại)",
                                    "target") from e
            if "403" in msg or "401" in msg:
                raise ProviderError(503, f"Microsoft từ chối Edge TTS (có thể đã bị chặn): {msg}", "account", 600) from e
            raise ProviderError(502, f"Edge TTS lỗi ({name}): {msg}") from e
        if not audio:
            raise ProviderError(502, "Edge TTS trả về âm thanh rỗng")
        # Edge TTS luôn trả MP3 24kHz; định dạng khác vẫn trả MP3 kèm content-type đúng
        return bytes(audio), "audio/mpeg"

    async def list_models(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[str]:
        lib = _lib()
        try:
            voices = await lib.list_voices()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(502, f"Không lấy được danh sách giọng Edge: {e}") from e
        names = [v.get("ShortName") for v in voices if isinstance(v, dict) and isinstance(v.get("ShortName"), str)]
        vi = sorted(n for n in names if n.startswith("vi-VN"))
        multi = sorted(n for n in names if "Multilingual" in n and n not in vi)
        en = sorted(n for n in names if n.startswith("en-US") and n not in multi)[:10]
        return vi + multi + en
