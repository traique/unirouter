"""SQLite storage + bộ nhớ đệm trong RAM.

Chạy 1 process duy nhất nên cache trong RAM luôn đồng bộ: mọi thay đổi từ /admin
đều đi qua Store và làm mới cache ngay.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .providers.catalog import PRESETS

PRESET_BY_ID = {p["id"]: p for p in PRESETS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    models TEXT NOT NULL DEFAULT '[]',
    headers TEXT NOT NULL DEFAULT '{}',
    options TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    secret_enc TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_used_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS combos (
    name TEXT PRIMARY KEY,
    strategy TEXT NOT NULL DEFAULT 'priority',
    targets TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    allowed_models TEXT,
    rpm INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_used_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    key_name TEXT,
    endpoint TEXT,
    requested_model TEXT,
    provider TEXT,
    model TEXT,
    connection TEXT,
    status INTEGER,
    latency_ms INTEGER,
    ttfb_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    rtk_saved INTEGER,
    attempts INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS device_logins (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    device_auth_id TEXT NOT NULL,
    user_code TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    label TEXT,
    status TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "rtk_enabled": True,
    "default_model": "",            # model/combo dùng khi client gửi model lạ
    "unknown_model_fallback": True,  # model lạ -> default_model thay vì báo lỗi
    "usage_retention_days": 7,
    "usage_max_rows": 50000,
    "breaker_threshold": 3,
    "breaker_cooldown": 60,
    "log_errors": True,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Crypto:
    def __init__(self, key: bytes):
        self._f = Fernet(key)

    def enc(self, data: Any) -> str:
        return self._f.encrypt(json.dumps(data).encode()).decode()

    def dec(self, token: str) -> Any:
        try:
            return json.loads(self._f.decrypt(token.encode()).decode())
        except (InvalidToken, ValueError) as e:
            raise RuntimeError("Không giải mã được secret: SECRET_KEY đã đổi?") from e


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.crypto = Crypto(config.FERNET_KEY)
        self._seed()
        self.reload()

    # ------------------------------------------------------------------ utils
    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple | list = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    def _seed(self) -> None:
        for p in PRESETS:
            exists = self.q("SELECT 1 FROM providers WHERE id=?", (p["id"],))
            if exists:
                continue
            self.x(
                "INSERT INTO providers (id,alias,name,type,base_url,models,headers,options,note,enabled,builtin,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,1,?)",
                (p["id"], p["alias"], p["name"], p["type"], p["base_url"], json.dumps(p.get("models", [])),
                 json.dumps(p.get("headers", {})), json.dumps(p.get("options", {})), p.get("note", ""), now_ms()),
            )
        for k, v in DEFAULT_SETTINGS.items():
            self.x("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, json.dumps(v)))

    # ------------------------------------------------------------------ cache
    def reload(self) -> None:
        with self.lock:
            self.settings = {r["key"]: json.loads(r["value"]) for r in self.q("SELECT * FROM settings")}
            self.providers: dict[str, dict[str, Any]] = {}
            self.alias_map: dict[str, str] = {}
            for r in self.q("SELECT * FROM providers ORDER BY builtin DESC, created_at"):
                p = dict(r)
                p["models"] = json.loads(p["models"])
                p["headers"] = json.loads(p["headers"])
                p["options"] = json.loads(p["options"])
                # tùy chọn mặc định mới của provider dựng sẵn (vd embedding_model) áp cho DB cũ
                preset_opts = PRESET_BY_ID.get(p["id"], {}).get("options") or {}
                p["options"] = {**{k: v for k, v in preset_opts.items() if k not in p["options"]}, **p["options"]}
                p["enabled"] = bool(p["enabled"])
                preset = PRESET_BY_ID.get(p["id"], {})
                p["signup_url"] = preset.get("signup_url", "")
                p["free"] = bool(preset.get("free"))
                self.providers[p["id"]] = p
                self.alias_map[p["alias"].lower()] = p["id"]
                self.alias_map[p["id"].lower()] = p["id"]
            self.connections: dict[str, dict[str, Any]] = {}
            for r in self.q("SELECT * FROM connections ORDER BY priority, created_at"):
                c = dict(r)
                c["meta"] = json.loads(c["meta"])
                c["enabled"] = bool(c["enabled"])
                self.connections[c["id"]] = c
            self.combos: dict[str, dict[str, Any]] = {}
            for r in self.q("SELECT * FROM combos"):
                c = dict(r)
                c["targets"] = json.loads(c["targets"])
                c["enabled"] = bool(c["enabled"])
                self.combos[c["name"].lower()] = c
            self.keys: dict[str, dict[str, Any]] = {}
            for r in self.q("SELECT * FROM api_keys"):
                k = dict(r)
                k["allowed_models"] = json.loads(k["allowed_models"]) if k["allowed_models"] else None
                k["enabled"] = bool(k["enabled"])
                self.keys[k["key_hash"]] = k

    def setting(self, key: str) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def set_settings(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                continue
            self.x("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (k, json.dumps(v)))
        self.reload()

    # -------------------------------------------------------------- providers
    def provider_connections(self, provider_id: str) -> list[dict[str, Any]]:
        return [c for c in self.connections.values() if c["provider_id"] == provider_id]

    def upsert_provider(self, data: dict[str, Any]) -> dict[str, Any]:
        pid = str(data.get("id") or "").strip().lower()
        if not pid or not pid.replace("-", "").replace("_", "").isalnum():
            raise ValueError("id provider chỉ gồm chữ, số, - và _")
        existing = self.providers.get(pid)
        alias = str(data.get("alias") or (existing or {}).get("alias") or pid).strip().lower()
        owner = self.alias_map.get(alias)
        if owner and owner != pid:
            raise ValueError(f"alias '{alias}' đã được dùng bởi provider {owner}")
        if "/" in alias:
            raise ValueError("alias không được chứa dấu /")
        ptype = data.get("type") or (existing or {}).get("type") or "openai"
        if ptype not in ("openai", "codex", "azure", "edge"):
            raise ValueError("type phải là openai, codex, azure hoặc edge")
        models = data.get("models", (existing or {}).get("models", []))
        if isinstance(models, str):
            models = [m.strip() for m in models.replace("\n", ",").split(",") if m.strip()]
        row = (
            alias, str(data.get("name") or (existing or {}).get("name") or pid), ptype,
            str(data.get("base_url") or (existing or {}).get("base_url") or "").rstrip("/"),
            json.dumps(models), json.dumps(data.get("headers", (existing or {}).get("headers", {}))),
            json.dumps(data.get("options", (existing or {}).get("options", {}))),
            str(data.get("note", (existing or {}).get("note", ""))),
            1 if data.get("enabled", (existing or {}).get("enabled", True)) else 0,
        )
        if not row[3]:
            raise ValueError("base_url là bắt buộc")
        if existing:
            self.x("UPDATE providers SET alias=?,name=?,type=?,base_url=?,models=?,headers=?,options=?,note=?,enabled=? WHERE id=?",
                   (*row, pid))
        else:
            self.x("INSERT INTO providers (alias,name,type,base_url,models,headers,options,note,enabled,id,builtin,created_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,0,?)", (*row, pid, now_ms()))
        self.reload()
        return self.providers[pid]

    def delete_provider(self, pid: str) -> None:
        p = self.providers.get(pid)
        if not p:
            raise KeyError(pid)
        if p["builtin"]:
            raise ValueError("Provider dựng sẵn chỉ tắt được, không xóa được")
        self.x("DELETE FROM connections WHERE provider_id=?", (pid,))
        self.x("DELETE FROM providers WHERE id=?", (pid,))
        self.reload()

    # ------------------------------------------------------------ connections
    def add_connection(self, provider_id: str, label: str, secret: dict[str, Any], meta: dict[str, Any] | None = None,
                       priority: int = 0) -> dict[str, Any]:
        if provider_id not in self.providers:
            raise KeyError(provider_id)
        cid = uuid.uuid4().hex[:12]
        self.x("INSERT INTO connections (id,provider_id,label,secret_enc,meta,enabled,priority,created_at) VALUES (?,?,?,?,?,1,?,?)",
               (cid, provider_id, label[:100], self.crypto.enc(secret), json.dumps(meta or {}), priority, now_ms()))
        self.reload()
        return self.connections[cid]

    def connection_secret(self, cid: str) -> dict[str, Any]:
        return self.crypto.dec(self.connections[cid]["secret_enc"])

    def update_connection_secret(self, cid: str, secret: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        with self.lock:
            c = self.connections.get(cid)
            if not c:
                return
            enc = self.crypto.enc(secret)
            if meta is not None:
                self.x("UPDATE connections SET secret_enc=?, meta=? WHERE id=?", (enc, json.dumps(meta), cid))
                c["meta"] = meta
            else:
                self.x("UPDATE connections SET secret_enc=? WHERE id=?", (enc, cid))
            c["secret_enc"] = enc

    def update_connection(self, cid: str, data: dict[str, Any]) -> None:
        c = self.connections.get(cid)
        if not c:
            raise KeyError(cid)
        label = str(data.get("label", c["label"]))[:100]
        enabled = 1 if data.get("enabled", c["enabled"]) else 0
        priority = int(data.get("priority", c["priority"]))
        self.x("UPDATE connections SET label=?, enabled=?, priority=? WHERE id=?", (label, enabled, priority, cid))
        if "api_key" in data and data["api_key"]:
            self.update_connection_secret(cid, {"api_key": str(data["api_key"]).strip()})
        if data.get("enabled"):
            self.x("UPDATE connections SET last_error=NULL WHERE id=?", (cid,))
        self.reload()

    def set_connection_error(self, cid: str, error: str | None) -> None:
        c = self.connections.get(cid)
        if not c or c.get("last_error") == error:
            return
        c["last_error"] = error
        self.x("UPDATE connections SET last_error=? WHERE id=?", (error[:500] if error else None, cid))

    def touch_connection(self, cid: str) -> None:
        c = self.connections.get(cid)
        if c:
            c["last_used_at"] = now_ms()

    def delete_connection(self, cid: str) -> None:
        self.x("DELETE FROM connections WHERE id=?", (cid,))
        self.reload()

    # ----------------------------------------------------------------- combos
    def upsert_combo(self, data: dict[str, Any]) -> None:
        name = str(data.get("name") or "").strip()
        if not name or "/" in name:
            raise ValueError("Tên combo bắt buộc và không chứa dấu /")
        strategy = data.get("strategy", "priority")
        if strategy not in ("priority", "round_robin", "least_latency", "random"):
            raise ValueError("strategy không hợp lệ")
        targets = data.get("targets") or []
        if isinstance(targets, str):
            targets = [t.strip() for t in targets.replace("\n", ",").split(",") if t.strip()]
        if not targets:
            raise ValueError("Combo cần ít nhất 1 model đích (dạng provider/model)")
        for t in targets:
            if "/" not in t:
                raise ValueError(f"'{t}' phải có dạng provider/model")
        old = data.get("old_name")
        if old and old.lower() != name.lower():
            self.x("DELETE FROM combos WHERE name=? COLLATE NOCASE", (old,))
        self.x("DELETE FROM combos WHERE name=? COLLATE NOCASE", (name,))
        self.x("INSERT INTO combos (name,strategy,targets,enabled,note,created_at) VALUES (?,?,?,?,?,?)",
               (name, strategy, json.dumps(targets), 1 if data.get("enabled", True) else 0, str(data.get("note", "")), now_ms()))
        self.reload()

    def delete_combo(self, name: str) -> None:
        self.x("DELETE FROM combos WHERE name=? COLLATE NOCASE", (name,))
        self.reload()

    # ------------------------------------------------------------------- keys
    def create_key(self, name: str, allowed_models: list[str] | None, rpm: int) -> str:
        raw = "sk-uni-" + secrets.token_urlsafe(32)
        self.x("INSERT INTO api_keys (id,name,key_hash,prefix,allowed_models,rpm,enabled,created_at) VALUES (?,?,?,?,?,?,1,?)",
               (uuid.uuid4().hex[:12], name[:100], hash_key(raw), raw[:14], json.dumps(allowed_models) if allowed_models else None,
                max(0, int(rpm)), now_ms()))
        self.reload()
        return raw

    def update_key(self, kid: str, data: dict[str, Any]) -> None:
        row = next((k for k in self.keys.values() if k["id"] == kid), None)
        if not row:
            raise KeyError(kid)
        allowed = data.get("allowed_models", row["allowed_models"])
        if isinstance(allowed, str):
            allowed = [m.strip() for m in allowed.split(",") if m.strip()] or None
        self.x("UPDATE api_keys SET name=?, allowed_models=?, rpm=?, enabled=? WHERE id=?",
               (str(data.get("name", row["name"]))[:100], json.dumps(allowed) if allowed else None,
                max(0, int(data.get("rpm", row["rpm"]))), 1 if data.get("enabled", row["enabled"]) else 0, kid))
        self.reload()

    def delete_key(self, kid: str) -> None:
        self.x("DELETE FROM api_keys WHERE id=?", (kid,))
        self.reload()

    def lookup_key(self, raw: str) -> dict[str, Any] | None:
        k = self.keys.get(hash_key(raw))
        if k and k["enabled"]:
            k["last_used_at"] = now_ms()
            return k
        return None

    # ------------------------------------------------------------------ usage
    def insert_usage(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT INTO usage (ts,key_name,endpoint,requested_model,provider,model,connection,status,latency_ms,ttfb_ms,"
                "input_tokens,output_tokens,rtk_saved,attempts,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def prune_usage(self) -> None:
        days = int(self.setting("usage_retention_days") or 7)
        self.x("DELETE FROM usage WHERE ts < ?", (now_ms() - days * 86400_000,))
        max_rows = int(self.setting("usage_max_rows") or 50000)
        self.x("DELETE FROM usage WHERE id <= (SELECT id FROM usage ORDER BY id DESC LIMIT 1 OFFSET ?)", (max_rows,))
        # Lưu lại last_used_at của key/connection (chỉ giữ trong RAM giữa các lần prune)
        for k in self.keys.values():
            if k.get("last_used_at"):
                self.x("UPDATE api_keys SET last_used_at=? WHERE id=?", (k["last_used_at"], k["id"]))
        for c in self.connections.values():
            if c.get("last_used_at"):
                self.x("UPDATE connections SET last_used_at=? WHERE id=?", (c["last_used_at"], c["id"]))
