"""Đối tượng dùng chung của ứng dụng (khởi tạo một lần)."""
from __future__ import annotations

from . import config
from .db import Store
from .health import Health, RateLimiter
from .providers.codex import CodexProvider
from .providers.openai_compat import OpenAICompatProvider
from .router import Router
from .usage import UsageLog


class AppState:
    def __init__(self, db_path: str | None = None):
        self.store = Store(str(db_path or config.DB_PATH))
        self.health = Health(threshold=int(self.store.setting("breaker_threshold")),
                             cooldown=float(self.store.setting("breaker_cooldown")))
        self.providers = {"openai": OpenAICompatProvider(), "codex": CodexProvider(self.store)}
        self.router = Router(self.store, self.providers, self.health)
        self.usage = UsageLog(self.store)
        self.limiter = RateLimiter()

    def apply_settings(self) -> None:
        self.health.threshold = int(self.store.setting("breaker_threshold"))
        self.health.cooldown = float(self.store.setting("breaker_cooldown"))
