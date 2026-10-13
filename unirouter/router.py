"""Định tuyến: model/combo -> danh sách đích -> chọn tài khoản -> fallback.

- Combo (alias) với 4 chiến lược: priority, round_robin, least_latency, random
- Nhiều tài khoản cho mỗi provider, xoay vòng hoặc dùng lần lượt
- Circuit breaker + cooldown theo Retry-After / thời điểm reset hạn mức
- Chỉ đổi provider khi CHƯA gửi dữ liệu nào cho client
- Embeddings và tạo ảnh dùng chung cơ chế chọn đích/fallback (Router.call)
"""
from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .health import Health
from .providers.base import CallContext, Event, Provider, ProviderError, Target, model_kind


AUTO_MODEL = "auto"


class GatewayError(Exception):
    def __init__(self, status: int, message: str, attempts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.attempts = attempts or []


@dataclass
class Stream:
    first: Event
    rest: AsyncIterator[Event]
    target: Target
    connection: dict[str, Any]
    started: float
    ttfb_ms: int
    attempts: list[dict[str, Any]] = field(default_factory=list)

    async def events(self) -> AsyncIterator[Event]:
        yield self.first
        async for ev in self.rest:
            yield ev

    async def aclose(self) -> None:
        try:
            await self.rest.aclose()  # type: ignore[attr-defined]
        except Exception:
            pass


class Router:
    def __init__(self, store: Any, providers: dict[str, Provider], health: Health):
        self.store = store
        self.impls = providers
        self.health = health

    # ------------------------------------------------------------ resolution
    def _target(self, pid: str, model: str) -> Target:
        p = self.store.providers[pid]
        return Target(p, model, f"{p['alias']}/{model}")

    def resolve(self, name: str, _depth: int = 0, allow_default: bool = True,
                kind: str = "chat") -> tuple[list[Target], str]:
        """Trả về (danh sách đích theo thứ tự thử, chiến lược)."""
        name = (name or "").strip()
        store = self.store
        if not name:
            if kind != "chat":
                name = AUTO_MODEL  # tạo ảnh/embeddings: client hay bỏ trống model
            elif _depth == 0 and allow_default:
                return self.resolve(store.setting("default_model") or AUTO_MODEL, 1)
            else:
                raise GatewayError(400, "Thiếu tham số model")

        combo = store.combos.get(name.lower())
        if kind != "chat" and name.lower() == AUTO_MODEL:
            combo = None  # combo tên "auto" là cho chat; embeddings/ảnh dùng auto theo loại
        if combo and combo["enabled"]:
            targets: list[Target] = []
            for spec in combo["targets"]:
                try:
                    sub, _ = self.resolve(spec, _depth + 1, kind=kind) if _depth < 3 else ([], "")
                except GatewayError:
                    continue
                targets.extend(sub)
            return self._order(targets, combo["strategy"], f"combo:{combo['name']}"), combo["strategy"]

        if name.lower() == AUTO_MODEL and (kind != "chat" or AUTO_MODEL not in store.combos):
            targets = self.auto_targets(kind)
            if not targets:
                if kind == "embeddings":
                    raise GatewayError(503, "Chưa có provider nào có model embedding. Thêm key NVIDIA, Gemini, Mistral "
                                            "hoặc GitHub Models, hoặc đặt \"Model embedding\" khi sửa provider.")
                if kind == "images":
                    raise GatewayError(503, "Chưa có provider nào tạo được ảnh. Đăng nhập ChatGPT hoặc đặt "
                                            "\"Model tạo ảnh\" khi sửa provider.")
                raise GatewayError(503, "Chưa có provider nào có API key/tài khoản. Vào /admin → Providers để thêm.")
            return targets, "priority"

        if "/" in name:
            alias, model = name.split("/", 1)
            pid = store.alias_map.get(alias.lower())
            if pid:
                return [self._target(pid, model)], "priority"

        # Tên model trần: tìm trong danh sách model của các provider
        matches = [self._target(pid, name) for pid, p in store.providers.items()
                   if p["enabled"] and (name in p["models"] or name == self.kind_model(p, kind))]
        if matches:
            return matches, "priority"

        if kind != "chat":
            if _depth == 0 and allow_default and store.setting("unknown_model_fallback"):
                return self.resolve(AUTO_MODEL, 1, kind=kind)
            raise GatewayError(404, f"Không tìm thấy model '{name}'. Dùng dạng provider/model hoặc \"auto\".")
        default = store.setting("default_model") or AUTO_MODEL
        if _depth == 0 and allow_default and store.setting("unknown_model_fallback") and default.lower() != name.lower():
            return self.resolve(default, 1)
        raise GatewayError(404, f"Không tìm thấy model hoặc combo '{name}'. Dùng dạng provider/model "
                                f"(vd: cx/gpt-5.5) hoặc tạo combo trong /admin.")

    @staticmethod
    def kind_model(p: dict[str, Any], kind: str) -> str | None:
        """Model provider dùng khi gọi "auto" cho từng loại (chat / embeddings / images)."""
        opts = p.get("options") or {}
        key = {"chat": "auto_model", "embeddings": "embedding_model", "images": "image_model"}[kind]
        if key in opts:
            return opts[key] or None  # chuỗi rỗng = tắt loại này cho provider
        found = next((m for m in p["models"] if model_kind(m) == kind), None)
        if found:
            return found
        if kind == "chat":
            return p["models"][0] if p["models"] else None
        if kind == "images" and p["type"] == "codex":
            return "gpt-image"
        return None

    def auto_targets(self, kind: str = "chat") -> list[Target]:
        """Model "auto": mọi provider đã có tài khoản, mỗi provider 1 model ưu tiên, thử lần lượt.

        Không cần tạo combo: thêm provider là dùng được ngay.
        """
        out: list[Target] = []
        for pid, p in self.store.providers.items():
            if not p["enabled"]:
                continue
            if not any(c["enabled"] for c in self.store.provider_connections(pid)):
                continue
            model = self.kind_model(p, kind)
            if model:
                out.append(self._target(pid, model))
        if kind == "images":  # tạo ảnh qua ChatGPT là thử nghiệm -> để cuối
            out.sort(key=lambda t: t.provider["type"] == "codex")
        return out

    def _order(self, targets: list[Target], strategy: str, key: str) -> list[Target]:
        if len(targets) < 2:
            return targets
        if strategy == "round_robin":
            i = self.health.next_rr(key, len(targets))
            return targets[i:] + targets[:i]
        if strategy == "random":
            t = list(targets)
            random.shuffle(t)
            return t
        if strategy == "least_latency":
            return sorted(targets, key=lambda t: self.health.latency(t.key))
        return targets

    def allowed(self, key: dict[str, Any] | None, requested: str) -> bool:
        if not key or not key.get("allowed_models"):
            return True
        req = requested.lower()
        return any(req == m.lower() or (m.endswith("*") and req.startswith(m[:-1].lower())) for m in key["allowed_models"])

    def _connections(self, provider: dict[str, Any]) -> list[dict[str, Any]]:
        conns = [c for c in self.store.provider_connections(provider["id"]) if c["enabled"]]
        conns.sort(key=lambda c: c["priority"])
        if len(conns) > 1 and (provider.get("options") or {}).get("account_strategy", "round_robin") == "round_robin":
            i = self.health.next_rr("acct:" + provider["id"], len(conns))
            conns = conns[i:] + conns[:i]
        return conns

    # -------------------------------------------------------------- execution
    async def open(self, ctx: CallContext, requested: str, allow_default: bool = True) -> Stream:
        # Key bị giới hạn model không được "lách" sang model mặc định
        targets, _ = self.resolve(requested, allow_default=allow_default)
        attempts: list[dict[str, Any]] = []
        last: ProviderError | None = None
        waits: list[float] = []

        for target in targets:
            provider = target.provider
            if not provider["enabled"]:
                attempts.append({"target": target.label, "error": "provider đang tắt"})
                continue
            impl = self.impls.get(provider["type"])
            if impl is None:
                continue
            if ctx.responses_body is not None and provider["type"] != "codex":
                # /v1/responses chỉ passthrough với Codex; provider khác dùng bản chuyển đổi
                ctx_use = CallContext(ctx.body, ctx.stream, ctx.endpoint, None, ctx.extras)
            else:
                ctx_use = ctx
            conns = self._connections(provider)
            if not conns:
                attempts.append({"target": target.label, "error": "chưa có tài khoản/API key"})
                continue
            for conn in conns:
                cid = conn["id"]
                if not self.health.available(cid):
                    waits.append(self.health.wait_seconds(cid))
                    attempts.append({"target": target.label, "account": conn["label"],
                                     "error": f"đang tạm khóa {int(self.health.wait_seconds(cid))}s"})
                    continue
                started = time.monotonic()
                try:
                    use = {**conn, "_secret": self.store.connection_secret(cid)}
                    it = await impl.open(target, use, ctx_use)
                    first = await it.__anext__()
                except StopAsyncIteration:
                    err = ProviderError(502, "Provider trả về phản hồi rỗng")
                    self._fail(target, conn, err, started, attempts)
                    last = err
                    break
                except ProviderError as err:
                    self._fail(target, conn, err, started, attempts)
                    last = err
                    if err.scope == "request":
                        raise GatewayError(err.status, err.message, attempts) from err
                    if err.scope == "account":
                        continue  # thử tài khoản khác của cùng provider
                    break  # thử đích tiếp theo
                except Exception as e:  # lỗi lạ: không để sập cả request
                    err = ProviderError(502, f"{type(e).__name__}: {e}")
                    self._fail(target, conn, err, started, attempts)
                    last = err
                    break
                ttfb = int((time.monotonic() - started) * 1000)
                self.store.touch_connection(cid)
                return Stream(first, it, target, conn, started, ttfb, attempts)

        if last is None and waits:
            raise GatewayError(429, f"Tất cả tài khoản đang bị giới hạn, thử lại sau {int(min(waits)) + 1}s", attempts)
        if last is None:
            detail = "; ".join(f"{a['target']}: {a['error']}" for a in attempts) or "không có đích khả dụng"
            raise GatewayError(503, f"Không có provider khả dụng cho '{requested}' ({detail})", attempts)
        status = last.status if last.status in (400, 401, 403, 404, 413, 422, 429) else 502
        if len(attempts) > 1:
            msg = f"Tất cả {len(attempts)} lượt thử đều lỗi. Lỗi cuối ({attempts[-1]['target']}): {last.message}"
        else:
            msg = last.message
        raise GatewayError(status, msg, attempts)

    async def call(self, kind: str, requested: str, body: dict[str, Any],
                   allow_default: bool = True) -> tuple[dict[str, Any], Target, dict[str, Any], list[dict[str, Any]], int]:
        """Gọi endpoint một lần (embeddings / tạo ảnh) với cùng cơ chế chọn đích, xoay tài khoản và fallback."""
        targets, _ = self.resolve(requested, allow_default=allow_default, kind=kind)
        attempts: list[dict[str, Any]] = []
        last: ProviderError | None = None
        waits: list[float] = []
        for target in targets:
            provider = target.provider
            impl = self.impls.get(provider["type"])
            if not provider["enabled"] or impl is None:
                attempts.append({"target": target.label, "error": "provider đang tắt"})
                continue
            conns = self._connections(provider)
            if not conns:
                attempts.append({"target": target.label, "error": "chưa có tài khoản/API key"})
                continue
            for conn in conns:
                cid = conn["id"]
                if not self.health.available(cid):
                    waits.append(self.health.wait_seconds(cid))
                    attempts.append({"target": target.label, "account": conn["label"],
                                     "error": f"đang tạm khóa {int(self.health.wait_seconds(cid))}s"})
                    continue
                started = time.monotonic()
                try:
                    use = {**conn, "_secret": self.store.connection_secret(cid)}
                    fn = impl.embeddings if kind == "embeddings" else impl.images
                    data = await fn(target, use, body)
                except ProviderError as err:
                    self._fail(target, conn, err, started, attempts)
                    last = err
                    if err.scope == "request":
                        raise GatewayError(err.status, err.message, attempts) from err
                    if err.scope == "account":
                        continue
                    break
                except Exception as e:  # noqa: BLE001 - lỗi lạ (thường do dữ liệu request): không phạt tài khoản
                    err = ProviderError(502, f"{type(e).__name__}: {e}")
                    attempts.append({"target": target.label, "account": conn["label"], "status": 502,
                                     "error": err.message[:300]})
                    last = err
                    break
                ms = int((time.monotonic() - started) * 1000)
                self.store.touch_connection(cid)
                self.health.success(cid)
                self.health.record_latency(target.key, ms, True)
                return data, target, conn, attempts, ms
        if last is None and waits:
            raise GatewayError(429, f"Tất cả tài khoản đang bị giới hạn, thử lại sau {int(min(waits)) + 1}s", attempts)
        if last is None:
            detail = "; ".join(f"{a['target']}: {a['error']}" for a in attempts) or "không có đích khả dụng"
            raise GatewayError(503, f"Không có provider khả dụng cho '{requested or 'auto'}' ({detail})", attempts)
        status = last.status if last.status in (400, 401, 403, 404, 413, 422, 429) else 502
        msg = (f"Tất cả {len(attempts)} lượt thử đều lỗi. Lỗi cuối ({attempts[-1]['target']}): {last.message}"
               if len(attempts) > 1 else last.message)
        raise GatewayError(status, msg, attempts)

    def _fail(self, target: Target, conn: dict[str, Any], err: ProviderError, started: float,
              attempts: list[dict[str, Any]]) -> None:
        attempts.append({"target": target.label, "account": conn["label"], "status": err.status, "error": err.message[:300]})
        if err.scope == "request":
            return
        if err.scope == "account":
            self.health.failure(conn["id"], err.message, err.cooldown)
            self.store.set_connection_error(conn["id"], f"[{err.status}] {err.message}"[:300])
        elif err.status >= 500:
            self.health.failure(conn["id"], err.message)
        self.health.record_latency(target.key, (time.monotonic() - started) * 1000, False)

    def done(self, stream: Stream, ok: bool, error: str | None = None) -> None:
        ms = (time.monotonic() - stream.started) * 1000
        self.health.record_latency(stream.target.key, stream.ttfb_ms if ok else ms, ok)
        cid = stream.connection["id"]
        if ok:
            self.health.success(cid)
            if stream.connection.get("last_error"):
                self.store.set_connection_error(cid, None)
        else:
            self.health.failure(cid, error or "stream error")
