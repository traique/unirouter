# UniRouter

Gateway AI gọn nhẹ, gộp ưu điểm của **9router**, **OmniRoute**, **apirouter** và **chatgpt-gateway** thành một dự án Python duy nhất. Chạy tốt trên gói Founder Free của botkeep.cloud: dùng khoảng 70–85 MB RAM (đo với 100 request đồng thời), 1 slot, lưu dữ liệu bằng SQLite.

```
Bot / Claude Code / Codex CLI / Cursor / app OpenAI
        │  OpenAI Chat · OpenAI Responses · Anthropic Messages
        ▼
┌──────────────────────── UniRouter ────────────────────────┐
│ API key riêng cho từng client · giới hạn model · giới hạn RPM │
│ RTK nén output tool (git diff, grep, log…)                 │
│ Combo/alias: priority · round-robin · nhanh nhất · ngẫu nhiên│
│ Nhiều tài khoản/provider · circuit breaker · cooldown 429   │
│ Chỉ đổi provider khi CHƯA gửi dữ liệu cho client          │
└───────────────────────────────────────────────────────────┘
        │
        ├─ ChatGPT (Codex OAuth: đăng nhập thiết bị hoặc nhập auth.json)
        ├─ NVIDIA NIM · Groq · OpenRouter · TokenRouter · Cerebras
        ├─ Gemini · Mistral · DeepSeek
        └─ Bất kỳ API tương thích OpenAI nào (thêm trong /admin)
```

## Lấy gì từ đâu

| Nguồn | Phần đã đưa vào UniRouter |
|---|---|
| chatgpt-gateway | Đăng nhập ChatGPT bằng mã thiết bị, `curl_cffi` giả lập trình duyệt, mã hóa token (Fernet), API key cho từng client, tự thử lại 1 lần khi `server_is_overloaded`, tương thích Anthropic `/v1/messages` |
| apirouter | Alias model kèm chuỗi dự phòng, circuit breaker, Retry-After, đổi provider trong suốt trước chunk đầu tiên, sửa cấu hình trực tiếp trên dashboard |
| 9router | Chuẩn hóa payload Codex (header CLI, allowlist field, `responses-lite`, reasoning effort qua hậu tố `-high`), khóa tài khoản tới lúc reset khi `usage_limit_reached`, refresh token sớm 10 phút (tránh xoay token), RTK Token Saver, xoay vòng nhiều tài khoản, xem hạn mức |
| OmniRoute | Nhiều chiến lược routing (ưu tiên, xoay vòng, độ trễ thấp nhất, ngẫu nhiên), dashboard thống kê theo model/key |

Đã lược bỏ để giữ nhẹ: build Next.js, Postgres/Supabase, Notion provider, MITM proxy, browser pool, TTS/ảnh/video.

## Chạy thử trên máy

```bash
pip install -r requirements.txt
cp .env.example .env              # sửa ADMIN_PASSWORD và SECRET_KEY
export $(grep -v '^#' .env | xargs)
python main.py                    # mở http://localhost:20128/admin
```

## Deploy lên botkeep.cloud

1. **Tạo server Python** trong panel: 1 slot, khoảng 256 MB RAM là đủ (còn dư slot và RAM cho bot khác).
2. **Đưa code lên** bằng ZIP (giải nén ra thư mục gốc, có `main.py` và `requirements.txt` ở ngoài cùng) hoặc kết nối GitHub.
3. **Lệnh khởi động:** `python main.py`. Nếu panel có ô "requirements file", điền `requirements.txt`.
4. **Biến môi trường (Secrets):**
   - `ADMIN_PASSWORD`: mật khẩu trang quản trị
   - `SECRET_KEY`: chuỗi ngẫu nhiên dài, tạo bằng `python -c "import secrets;print(secrets.token_urlsafe(48))"`. **Không đổi sau khi đã thêm tài khoản.**
   - Cổng: app tự đọc `PORT` hoặc `SERVER_PORT` (biến mà Pterodactyl/Botkeep cấp). Nếu panel cấp một cổng cố định, đặt `PORT=<cổng đó>`.
5. **Gắn domain** (mục Domains, có HTTPS) trỏ vào server, rồi mở `https://domain-cua-ban/admin`.
6. **Sao lưu:** toàn bộ dữ liệu nằm trong `data/unirouter.db` (và `data/secret.key` nếu không đặt `SECRET_KEY`). Dùng tính năng Backup của Botkeep, và tự giữ bản sao vì Botkeep chưa tự xóa backup cũ.

> Docs của Botkeep chưa ghi rõ cách mở cổng HTTP public cho app Python. Nếu panel có template "FastAPI API", hãy tạo server từ template đó rồi thay code. Kiểm tra bằng `https://domain/health`.

## Thiết lập lần đầu (trong /admin)

1. **Providers:** thêm API key (mỗi dòng 1 key để xoay vòng). Với ChatGPT, bấm **Đăng nhập ChatGPT**, mở link, nhập mã. Nếu OpenAI chặn IP máy chủ, chạy `codex login` trên máy bạn rồi dán `~/.codex/auth.json` vào mục **Nhập auth.json**.
2. Bấm **Test** trên từng tài khoản. Dùng **Sửa → Lấy danh sách model** để cập nhật model mới nhất.
3. **Combo:** ví dụ combo `gpt-4o` = `cx/gpt-5.5` → `nim/nvidia/nemotron-3-ultra-550b-a55b` → `groq/openai/gpt-oss-120b` → `or/…:free`. Bot cũ gọi `gpt-4o` vẫn chạy, tự chuyển provider khi lỗi hoặc hết hạn mức.
4. **API Keys:** tạo key cho từng bot (có thể giới hạn model, ví dụ `gpt-4o, cx/*`, và giới hạn request/phút).
5. **Cài đặt:** đặt *model mặc định* để mọi tên model lạ đều chạy qua combo đó.

## Gọi API

Tên model có dạng `alias/model` (`cx/gpt-5.5`, `nim/…`, `groq/…`, `or/…`), tên combo, hoặc tên model trần nếu chỉ một provider có model đó. Hậu tố `-low`, `-medium`, `-high` hoặc `-xhigh` đặt mức suy luận cho ChatGPT, ví dụ `cx/gpt-5.5-high`.

```bash
# OpenAI Chat Completions
curl https://domain/v1/chat/completions -H "Authorization: Bearer sk-uni-..." \
  -H "Content-Type: application/json" -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Xin chào"}],"stream":true}'
```

| Client | Cấu hình |
|---|---|
| OpenAI SDK, bot | `base_url=https://domain/v1`, `api_key=sk-uni-…` |
| Claude Code | `ANTHROPIC_BASE_URL=https://domain`, `ANTHROPIC_AUTH_TOKEN=sk-uni-…`, `ANTHROPIC_MODEL=<combo>` |
| Codex CLI | `model_providers.unirouter`: `base_url="https://domain/v1"`, `wire_api="responses"` |
| Anthropic SDK | `base_url="https://domain"`, `api_key="sk-uni-…"` |

Endpoint: `/v1/chat/completions`, `/v1/responses` (chuyển thẳng tới ChatGPT, tự chuyển đổi định dạng với provider khác), `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`, `/health`.

## Hành vi khi lỗi

| Lỗi upstream | Xử lý |
|---|---|
| 429, 401, 403, hết hạn mức | Khóa tạm tài khoản đó (theo Retry-After hoặc thời điểm reset), thử tài khoản khác cùng provider, rồi tới model kế tiếp |
| 5xx, timeout, lỗi mạng | Thử model kế tiếp. Lỗi liên tiếp vượt ngưỡng thì circuit breaker nghỉ N giây |
| 404, model không tồn tại, vượt context | Thử model kế tiếp |
| 400 do request sai (vd tham số) | Trả lỗi ngay, không đổi provider |
| Lỗi sau khi đã stream một phần | Báo lỗi trong stream (không đổi provider giữa chừng để tránh trả lời bị ghép) |

Khi model đang suy nghĩ lâu, gateway gửi `: keepalive` mỗi 15 giây để reverse proxy không cắt kết nối.

## Lưu ý về ChatGPT

Provider ChatGPT gọi endpoint nội bộ của Codex (`chatgpt.com/backend-api/codex`) bằng token đăng nhập, **không phải API chính thức của OpenAI**. Điều này có nghĩa:

- Phần này có thể ngừng chạy khi OpenAI đổi backend. Nếu gặp lỗi, xem log lỗi trên dashboard và cập nhật `CODEX_VERSION` trong `unirouter/providers/codex_auth.py`.
- Dùng tài khoản qua máy chủ trung gian, chia sẻ cho nhiều người hoặc chạy tần suất cao có thể vi phạm điều khoản và khiến tài khoản bị khóa. 9router cũng đánh dấu provider này là "rủi ro".
- Nên đặt ChatGPT là một tầng trong combo, luôn kèm các provider có API key thật phía sau.

## Phát triển

```bash
pip install pytest openai anthropic
python -m pytest tests -q     # 40 test: upstream giả lập + SDK openai/anthropic thật
```

Cấu trúc: `unirouter/providers/` (Codex, tương thích OpenAI), `unirouter/formats/` (chuyển đổi Chat ↔ Responses ↔ Anthropic), `router.py` (combo, fallback), `health.py` (breaker, rate limit), `rtk.py`, `db.py` (SQLite), `admin.py` và `static/admin.html` (dashboard).
