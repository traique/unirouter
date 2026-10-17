"""Kiểu dữ liệu chung cho mọi provider.

Mọi provider nhận request dạng OpenAI Chat Completions (dict) và trả về một luồng
sự kiện nội bộ (Event). Các bộ xuất (formats/*) biến luồng này thành SSE/JSON theo
định dạng mà client yêu cầu (OpenAI Chat, Responses, Anthropic Messages).

Event là dict với "type":
  text        {"text"}
  reasoning   {"text"}
  tool_start  {"index", "id", "name"}
  tool_args   {"index", "args"}
  usage       {"input", "output", "cached"?}
  finish      {"reason": stop|tool_calls|length|content_filter}
  raw         {"event": dict}   # chỉ dùng cho passthrough Responses của Codex
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

Event = dict[str, Any]


class ProviderError(Exception):
    """Lỗi từ upstream.

    scope:
      "request"    lỗi do request của client -> trả lỗi ngay, không fallback
      "account"    lỗi của tài khoản (401/403/429) -> thử tài khoản khác cùng provider
      "target"     model/provider này lỗi -> thử model đích tiếp theo
      "model"      provider không còn hỗ trợ model này (vd ChatGPT ngừng một model) -> bỏ model khỏi
                   danh sách của provider; nếu đích do "auto" chọn thì thử ngay model kế tiếp cùng provider
    """

    def __init__(self, status: int, message: str, scope: str = "target", cooldown: float = 0.0,
                 model: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.scope = scope
        self.cooldown = cooldown  # giây; >0 => tạm khóa tài khoản
        self.model = model        # scope "model": tên model (đã bỏ hậu tố effort) bị provider từ chối

    def __repr__(self) -> str:  # pragma: no cover
        return f"ProviderError({self.status}, {self.scope}, {self.message[:80]!r})"


def classify_http(status: int, message: str, retry_after: float = 0.0) -> ProviderError:
    low = message.lower()
    if status == 429:
        return ProviderError(status, message, "account", retry_after or 30)
    if status in (401, 403):
        return ProviderError(status, message, "account", retry_after or 300)
    if status == 402:
        return ProviderError(status, message, "account", 3600)
    if status in (404,):
        return ProviderError(status, message, "target")
    if status in (400, 422):
        # Lỗi model không tồn tại / không hỗ trợ ở provider này -> thử đích khác
        hints = ("model", "not supported", "unsupported", "not found", "does not exist", "decommission",
                 "context length", "context_length", "too long", "maximum context")
        if any(h in low for h in hints):
            return ProviderError(status, message, "target")
        return ProviderError(status, message, "request")
    if status == 413:
        return ProviderError(status, message, "target")
    return ProviderError(status or 502, message, "target")


@dataclass
class Target:
    provider: dict[str, Any]
    model: str
    label: str  # "alias/model" để hiển thị
    auto: bool = False  # model do "auto" chọn thay cho provider (không phải client chỉ định)

    @property
    def key(self) -> str:
        return f"{self.provider['id']}/{self.model}"


@dataclass
class CallContext:
    body: dict[str, Any]                  # request chuẩn OpenAI Chat
    stream: bool
    endpoint: str
    responses_body: dict[str, Any] | None = None  # request gốc nếu client gọi /v1/responses
    extras: dict[str, Any] = field(default_factory=dict)


class Provider:
    type = "base"

    async def open(self, target: Target, connection: dict[str, Any], ctx: CallContext) -> AsyncIterator[Event]:
        raise NotImplementedError

    async def list_models(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[str]:
        return [m["id"] for m in await self.list_models_detailed(provider, connection)]

    async def list_models_detailed(self, provider: dict[str, Any], connection: dict[str, Any]) -> list[dict[str, Any]]:
        """Danh sách model từ provider: [{"id", "free"}]. Model miễn phí xếp trước."""
        return [{"id": m, "free": False} for m in provider.get("models") or []]

    async def embeddings(self, target: Target, connection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        raise ProviderError(400, f"{target.provider['name']} không hỗ trợ embeddings", "target")

    async def images(self, target: Target, connection: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        raise ProviderError(400, f"{target.provider['name']} không hỗ trợ tạo ảnh", "target")


# --------------------------------------------------------------- loại model
KINDS = ("chat", "embeddings", "images")
_EMBED_HINTS = ("embed", "bge-", "/bge", "e5-", "-e5", "gte-", "nomic-embed", "rerank")
_IMAGE_HINTS = ("image", "imagen", "flux", "dall-e", "dalle", "stable-diffusion", "sdxl", "sd3", "kandinsky",
                "playground-v", "recraft", "ideogram", "seedream", "hidream")


def model_kind(model: str) -> str:
    """Đoán loại model theo tên: embeddings / images / chat."""
    m = model.lower()
    if any(h in m for h in _EMBED_HINTS):
        return "embeddings"
    # "gemini-2.5-flash-image" vẫn là model chat có xuất ảnh -> vẫn coi là ảnh
    if any(h in m for h in _IMAGE_HINTS):
        return "images"
    return "chat"
