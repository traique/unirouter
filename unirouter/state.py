"""Đối tượng dùng chung của ứng dụng (khởi tạo một lần)."""
from __future__ import annotations

from . import config
from .db import Store
from .health import Health, RateLimiter
from .providers.azure_speech import AzureSpeechProvider
from .providers.codex import CodexProvider
from .providers.edge_speech import EdgeSpeechProvider
from .providers.openai_compat import OpenAICompatProvider
from .router import Router
from .usage import UsageLog


class AppState:
    def __init__(self, db_path: str | None = None):
        self.store = Store(str(db_path or config.DB_PATH))
        self.health = Health(threshold=int(self.store.setting("breaker_threshold")),
                             cooldown=float(self.store.setting("breaker_cooldown")))
        self.providers = {"openai": OpenAICompatProvider(self._learn_option), "codex": CodexProvider(self.store),
                          "azure": AzureSpeechProvider(), "edge": EdgeSpeechProvider()}
        self.router = Router(self.store, self.providers, self.health)
        self.usage = UsageLog(self.store)
        self.limiter = RateLimiter()

    def _learn_option(self, provider_id: str, key: str, value) -> None:
        p = self.store.providers.get(provider_id)
        if p is not None:
            try:
                self.store.upsert_provider({"id": provider_id, "options": {**(p.get("options") or {}), key: value}})
            except Exception:
                pass

    def apply_settings(self) -> None:
        self.health.threshold = int(self.store.setting("breaker_threshold"))
        self.health.cooldown = float(self.store.setting("breaker_cooldown"))
