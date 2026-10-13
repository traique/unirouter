"""Lưu tạm ảnh đã tạo để trả về dạng URL (nhiều bot chỉ đọc data[0].url).

Ảnh nằm trong DATA_DIR/images với tên ngẫu nhiên khó đoán, tự xóa sau MAX_AGE giây
hoặc khi vượt MAX_FILES ảnh.
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from pathlib import Path

from . import config

DIR = config.DATA_DIR / "images"
MAX_AGE = 24 * 3600
MAX_FILES = 300
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}\.(png|jpg|webp|gif)$")
TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _ext(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return "png"
    if raw.startswith(b"\xff\xd8"):
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:3] == b"GIF":
        return "gif"
    return "png"


def cleanup() -> None:
    try:
        files = sorted(DIR.glob("*.*"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    now = time.time()
    for i, f in enumerate(files):
        try:
            if now - f.stat().st_mtime > MAX_AGE or i < len(files) - MAX_FILES:
                f.unlink()
        except OSError:
            pass


def save_b64(b64: str) -> str | None:
    """Ghi ảnh base64 ra file, trả về tên file (hoặc None nếu dữ liệu hỏng)."""
    try:
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    DIR.mkdir(parents=True, exist_ok=True)
    cleanup()
    name = f"{secrets.token_urlsafe(24)}.{_ext(raw)}"
    (DIR / name).write_bytes(raw)
    return name


def path_of(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    p = DIR / name
    try:
        if not p.is_file() or time.time() - p.stat().st_mtime > MAX_AGE:
            return None
    except OSError:
        return None
    return p


def media_type(name: str) -> str:
    return TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")
