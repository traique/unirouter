"""Ghi log sử dụng theo lô (giảm ghi đĩa) + thống kê cho dashboard."""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from . import images
from .db import Store, now_ms


class UsageLog:
    def __init__(self, store: Store):
        self.store = store
        self.queue: list[tuple] = []
        self.task: asyncio.Task | None = None
        self.started = time.time()
        self.total = 0

    def add(self, **row: Any) -> None:
        self.total += 1
        self.queue.append((
            now_ms(), row.get("key_name"), row.get("endpoint"), row.get("requested_model"), row.get("provider"),
            row.get("model"), row.get("connection"), row.get("status"), row.get("latency_ms"), row.get("ttfb_ms"),
            row.get("input_tokens"), row.get("output_tokens"), row.get("rtk_saved"), row.get("attempts"),
            (row.get("error") or None) and str(row["error"])[:500], row.get("key_id"),
        ))
        if len(self.queue) > 500:
            self.flush()

    def flush(self) -> None:
        rows, self.queue = self.queue, []
        if rows:
            try:
                self.store.insert_usage(rows)
            except Exception as e:  # pragma: no cover
                print(f"[usage] ghi log lỗi: {e}")

    async def run(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(3)
            self.flush()
            tick += 1
            if tick % 1200 == 1:  # ~1 giờ
                with contextlib.suppress(Exception):
                    self.store.prune_usage()
                with contextlib.suppress(Exception):
                    images.cleanup()

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.flush()

    def stats(self, hours: int = 24) -> dict[str, Any]:
        self.flush()
        since = now_ms() - hours * 3600_000
        q = self.store.q
        total = q("SELECT COUNT(*) n, SUM(status<400) ok, SUM(COALESCE(input_tokens,0)) inp, "
                  "SUM(COALESCE(output_tokens,0)) outp, SUM(COALESCE(rtk_saved,0)) rtk, AVG(latency_ms) lat "
                  "FROM usage WHERE ts>=?", (since,))[0]
        by_model = q("SELECT provider, model, COUNT(*) n, SUM(status<400) ok, SUM(COALESCE(input_tokens,0)) inp, "
                     "SUM(COALESCE(output_tokens,0)) outp, AVG(CASE WHEN status<400 THEN ttfb_ms END) ttfb "
                     "FROM usage WHERE ts>=? AND provider IS NOT NULL GROUP BY provider, model ORDER BY n DESC LIMIT 30",
                     (since,))
        by_key = q("SELECT key_name, COUNT(*) n, SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) tok "
                   "FROM usage WHERE ts>=? GROUP BY key_name ORDER BY n DESC LIMIT 20", (since,))
        bucket = 3600_000 if hours > 6 else 300_000
        series = q("SELECT (ts/?)*? b, COUNT(*) n, SUM(status>=400) err, "
                   "SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) tok FROM usage WHERE ts>=? GROUP BY b ORDER BY b",
                   (bucket, bucket, since))
        recent = q("SELECT * FROM usage ORDER BY id DESC LIMIT 60")
        return {
            "hours": hours,
            "uptime_s": int(time.time() - self.started),
            "total": dict(total),
            "by_model": [dict(r) for r in by_model],
            "by_key": [dict(r) for r in by_key],
            "series": [dict(r) for r in series],
            "bucket_ms": bucket,
            "recent": [dict(r) for r in recent],
        }
