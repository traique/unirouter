"""Task nền giữ phiên sống cho các provider dùng đăng nhập (không phải API key).

- Codex (ChatGPT): refresh token của OpenAI có thể hết hạn nếu TÀI KHOẢN để lâu không dùng.
  Ta refresh THƯA (chỉ khi lần refresh gần nhất đã quá ~7 ngày), vì mỗi lần refresh OpenAI xoay
  refresh token; refresh quá dày làm tăng nguy cơ dùng nhầm token cũ -> bị thu hồi cả phiên.
- Gemini Web (cookie): Google xoay __Secure-1PSIDTS liên tục; để lâu không xoay thì phiên chết.
  Ta chủ động xoay cookie định kỳ và LƯU lại vào DB.

Chạy nhẹ: thức dậy mỗi 10 phút, chỉ gọi mạng cho connection tới hạn. Với Gemini, mỗi lượt chỉ là
một POST RotateCookies nhỏ; tải trang /app để kiểm tra phiên còn sống chỉ làm mỗi giờ một lần. Lỗi được nuốt và ghi vào
last_error của connection để trang quản trị báo đỏ, không làm sập app.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from . import config
from .db import now_ms
from .providers import codex_auth, gemini_web_auth
from .providers.base import ProviderError

log = logging.getLogger("unirouter.keepalive")

TICK_SECONDS = 10 * 60
CODEX_REFRESH_AFTER_MS = 7 * 24 * 3600 * 1000  # chỉ refresh ChatGPT khi đã quá 7 ngày
GEMINI_ROTATE_AFTER_MS = 9 * 60 * 1000         # xoay cookie Gemini ~mỗi 10 phút như Chrome
GEMINI_VALIDATE_AFTER_MS = 60 * 60 * 1000      # kiểm tra phiên (tải trang) mỗi giờ
CODEX_FATAL_COOLDOWN = 10 ** 7                  # cooldown codex_auth gắn cho lỗi refresh không cứu được


class Keepalive:
    def __init__(self, store: Any):
        self.store = store
        self.task: asyncio.Task | None = None
        self._validated: dict[str, int] = {}  # connection id -> lần kiểm tra phiên gần nhất
        # connection id -> refresh token ChatGPT đã bị từ chối hẳn (thu hồi/hết hạn). Không thử lại token đó
        # mỗi 10 phút; đăng nhập lại (refresh token đổi) thì tự chạy lại.
        self._dead_refresh: dict[str, str] = {}

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # chờ app ổn định
        while True:
            with contextlib.suppress(Exception):
                await self._run_once()
            await asyncio.sleep(TICK_SECONDS)

    async def _run_once(self) -> None:
        now = now_ms()
        for c in list(self.store.connections.values()):
            provider = self.store.providers.get(c["provider_id"])
            # Tài khoản đang tắt: Codex không cần đụng; Gemini vẫn xoay cookie (rẻ) để phiên không chết
            # trong lúc tạm tắt, trừ khi nó tắt vì đã lỗi.
            if not c["enabled"] and (not provider or provider["type"] != "gemini_web" or c.get("last_error")):
                continue
            if not provider:
                continue
            try:
                if provider["type"] == "codex":
                    await self._codex(c, now)
                elif provider["type"] == "gemini_web":
                    await self._gemini(c, now)
            except ProviderError as e:
                self.store.set_connection_error(c["id"], f"[{e.status}] {e.message}"[:300])
                log.info("keepalive %s: %s", c["label"], e.message[:160])
            except Exception as e:  # noqa: BLE001
                log.info("keepalive %s lỗi: %s", c["label"], e)

    async def _codex(self, c: dict[str, Any], now: int) -> None:
        secret = self.store.connection_secret(c["id"])
        if not secret.get("refresh_token"):
            return
        if self._dead_refresh.get(c["id"]) == secret["refresh_token"]:
            return
        last = int(secret.get("refreshed_at") or 0)
        if last and now - last < CODEX_REFRESH_AFTER_MS:
            return
        # ensure_access_token(force=True) đi qua cùng khóa + lưu token mới, an toàn với request song song.
        try:
            await codex_auth.ensure_access_token(self.store, c, force=True)
        except ProviderError as e:
            if e.cooldown >= CODEX_FATAL_COOLDOWN:
                self._dead_refresh[c["id"]] = secret["refresh_token"]
            raise
        self._dead_refresh.pop(c["id"], None)

    async def _gemini(self, c: dict[str, Any], now: int) -> None:
        secret = self.store.connection_secret(c["id"])
        if "cookies" not in secret:
            return
        proxy = config.GEMINI_WEB_PROXY or None
        if c["enabled"] and now - self._validated.get(c["id"], 0) >= GEMINI_VALIDATE_AFTER_MS:
            # Xoay + tải lại trang: phát hiện phiên chết sớm để trang quản trị báo đỏ, và làm mới token trang.
            self._validated[c["id"]] = now
            await gemini_web_auth.ensure_session(self.store, c, proxy, force_rotate=True, refresh_page=True)
            return
        last = int(secret.get("rotated_at") or 0)
        if now - last >= GEMINI_ROTATE_AFTER_MS:
            await gemini_web_auth.rotate_and_save(self.store, c, proxy)
