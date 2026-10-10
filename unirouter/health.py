"""Circuit breaker, cooldown tài khoản, độ trễ (EWMA), rate limit theo key. Tất cả trong RAM."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ConnState:
    failures: int = 0
    open_until: float = 0.0       # circuit breaker mở (do lỗi liên tiếp)
    cooldown_until: float = 0.0   # bị provider giới hạn (429, hết quota...)
    half_open: bool = False
    last_reason: str = ""


@dataclass
class TargetStats:
    ewma_ms: float = 0.0
    ok: int = 0
    fail: int = 0
    last_ok: float = 0.0


@dataclass
class Health:
    threshold: int = 3
    cooldown: float = 60.0
    conns: dict[str, ConnState] = field(default_factory=dict)
    targets: dict[str, TargetStats] = field(default_factory=dict)
    rr: dict[str, int] = field(default_factory=dict)

    def conn(self, cid: str) -> ConnState:
        return self.conns.setdefault(cid, ConnState())

    def available(self, cid: str, now: float | None = None) -> bool:
        now = now or time.time()
        st = self.conn(cid)
        if st.cooldown_until > now:
            return False
        if st.open_until > now:
            return False
        if st.open_until and not st.half_open:
            st.half_open = True  # cho 1 request thử
        return True

    def wait_seconds(self, cid: str) -> float:
        st = self.conn(cid)
        return max(0.0, max(st.cooldown_until, st.open_until) - time.time())

    def success(self, cid: str) -> None:
        st = self.conn(cid)
        st.failures = 0
        st.open_until = 0.0
        st.half_open = False
        st.last_reason = ""

    def failure(self, cid: str, reason: str, cooldown: float = 0.0) -> None:
        st = self.conn(cid)
        now = time.time()
        st.last_reason = reason[:200]
        if cooldown > 0:
            st.cooldown_until = max(st.cooldown_until, now + cooldown)
            return
        st.failures += 1
        if st.half_open or st.failures >= self.threshold:
            st.open_until = now + self.cooldown
            st.half_open = False

    def reset(self, cid: str) -> None:
        self.conns.pop(cid, None)

    def record_latency(self, key: str, ms: float, ok: bool) -> None:
        s = self.targets.setdefault(key, TargetStats())
        if ok:
            s.ok += 1
            s.last_ok = time.time()
            s.ewma_ms = ms if not s.ewma_ms else 0.3 * ms + 0.7 * s.ewma_ms
        else:
            s.fail += 1

    def latency(self, key: str) -> float:
        s = self.targets.get(key)
        if not s:
            return 0.0  # chưa có dữ liệu -> ưu tiên thử
        if not s.ewma_ms:
            return 1e9 + s.fail if s.fail else 0.0  # chỉ toàn lỗi -> xếp cuối
        penalty = 1.0 + (s.fail / max(1, s.ok + s.fail))
        return s.ewma_ms * penalty

    def next_rr(self, key: str, n: int) -> int:
        i = self.rr.get(key, 0)
        self.rr[key] = (i + 1) % max(1, n)
        return i % max(1, n)


class RateLimiter:
    def __init__(self) -> None:
        self.windows: dict[str, deque[float]] = {}

    def allow(self, key: str, rpm: int) -> tuple[bool, float]:
        if rpm <= 0:
            return True, 0.0
        now = time.time()
        q = self.windows.setdefault(key, deque())
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= rpm:
            return False, 60 - (now - q[0])
        q.append(now)
        return True, 0.0
