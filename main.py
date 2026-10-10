"""Điểm khởi động: `python main.py` (Botkeep, VPS, Docker đều dùng lệnh này)."""
import uvicorn

from unirouter import config
from unirouter.app import create_app

app = create_app()

if __name__ == "__main__":
    print(f"UniRouter {config.VERSION} chạy tại http://{config.HOST}:{config.PORT}  (admin: /admin)")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level=config.LOG_LEVEL, proxy_headers=True,
                forwarded_allow_ips="*", timeout_keep_alive=75, access_log=config.LOG_LEVEL == "debug")
