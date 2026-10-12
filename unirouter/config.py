"""Cấu hình từ biến môi trường. Mọi thứ khác chỉnh trong /admin và lưu SQLite."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

VERSION = "1.2.1"


def load_dotenv(*paths: Path) -> list[str]:
    """Đọc file .env (Botkeep ghi biến môi trường vào .env thay vì truyền vào process).

    Không ghi đè biến đã có sẵn trong môi trường. Hỗ trợ KEY=VALUE, `export KEY=...`,
    giá trị trong nháy đơn/kép, và bỏ qua dòng trống/chú thích.
    """
    loaded: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded


_ROOT = Path(__file__).resolve().parent.parent
DOTENV_LOADED = load_dotenv(Path.cwd() / ".env", _ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DATA_DIR = Path(_env("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "unirouter.db"

HOST = _env("HOST", "0.0.0.0")
# Botkeep/Pterodactyl thường cấp cổng qua SERVER_PORT; các nền tảng khác dùng PORT.
PORT = int(_env("PORT") or _env("SERVER_PORT") or "20128")

ADMIN_USERNAME = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
# Key "master" dùng được cho mọi model (tùy chọn). Key cho từng client tạo trong /admin.
MASTER_API_KEY = _env("MASTER_API_KEY")
COOKIE_SECURE = _env("COOKIE_SECURE", "auto").lower()  # auto | true | false

REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT", "300"))
CONNECT_TIMEOUT = float(_env("CONNECT_TIMEOUT", "15"))
LOG_LEVEL = _env("LOG_LEVEL", "info")


def _load_secret() -> str:
    """SECRET_KEY dùng ký cookie admin và mã hóa token trong SQLite.

    Nên đặt qua biến môi trường. Nếu không có, tạo ngẫu nhiên và lưu vào
    data/secret.key để giữ ổn định giữa các lần khởi động lại.
    """
    value = _env("SECRET_KEY")
    if value:
        return value
    path = DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


SECRET_KEY = _load_secret()
SECRET_FROM_ENV = bool(_env("SECRET_KEY"))
FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(("enc:" + SECRET_KEY).encode()).digest())
