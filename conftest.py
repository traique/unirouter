import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="unirouter-test-")
os.environ.update({"DATA_DIR": TMP, "MASTER_API_KEY": "master-test", "ADMIN_PASSWORD": "pw", "ADMIN_USERNAME": "admin",
                   "SECRET_KEY": "test-secret", "COOKIE_SECURE": "false"})

import uvicorn  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server không khởi động")


@pytest.fixture(scope="session")
def env():
    from tests import mock_upstream
    from unirouter.app import create_app
    from unirouter.providers import codex_auth

    up_port = free_port()
    serve(mock_upstream.app, up_port)
    up = f"http://127.0.0.1:{up_port}"
    codex_auth.AUTH_BASE = up + "/auth"

    app = create_app()
    st = app.state.app
    st.store.upsert_provider({"id": "mock", "alias": "mk", "name": "Mock", "type": "openai", "base_url": up + "/oai/v1",
                              "models": ["ok", "tool", "fail500", "rate429", "badreq", "nomodel", "slow", "midfail",
                                         "streamerr", "nostream"]})
    st.store.upsert_provider({"id": "mock2", "alias": "mk2", "name": "Mock 2", "type": "openai",
                              "base_url": up + "/oai/v1", "models": ["ok"]})
    st.store.add_connection("mock", "good", {"api_key": "good"})
    st.store.add_connection("mock2", "good2", {"api_key": "good"})
    st.store.upsert_provider({"id": "codex", "base_url": up + "/codex/responses",
                              "models": ["gpt-ok", "gpt-tool", "gpt-overload", "gpt-limit", "gpt-slow"]})
    # access token sắp hết hạn -> buộc refresh qua mock /auth/oauth/token
    st.store.add_connection("codex", "chatgpt-1", {"access_token": mock_upstream.access_token(60), "refresh_token": "rt-1",
                                                   "expires_at": int(time.time() * 1000) + 60_000},
                            {"account_id": "acc-123"})
    port = free_port()
    serve(app, port)
    return {"base": f"http://127.0.0.1:{port}", "app": app, "st": st, "mock": mock_upstream, "up": up}
