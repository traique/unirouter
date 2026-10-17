# UniRouter

Cổng AI tự host: gom ChatGPT và các nhà cung cấp model (NVIDIA NIM, Groq, OpenRouter, Gemini, Mistral, GitHub Models…) về **một địa chỉ, một API key**. Bot, Claude Code, Codex CLI hay bất kỳ app nào dùng OpenAI SDK đều gọi được, và khi một nguồn lỗi hoặc hết hạn mức thì UniRouter tự chuyển sang nguồn khác.

Viết bằng Python (FastAPI), một tiến trình, dữ liệu trong một file SQLite. Chạy thoải mái trên VPS nhỏ hoặc gói hosting miễn phí: khoảng 70–90 MB RAM.

```
 Bot · Claude Code · Codex CLI · Cursor · n8n · app OpenAI SDK
                         │
      Chat Completions · Responses · Anthropic Messages
      Embeddings · Tạo ảnh
                         ▼
 ┌──────────────────────── UniRouter ────────────────────────┐
 │ API key cho từng client · giới hạn model · giới hạn RPM   │
 │ model "auto" · combo: ưu tiên / xoay vòng / nhanh nhất    │
 │ nhiều key mỗi provider · tạm khóa key lỗi · tự chuyển     │
 │ nén output tool dài (RTK) · thống kê · trang quản trị     │
 └───────────────────────────────────────────────────────────┘
                         │
   ChatGPT (đăng nhập tài khoản) · NVIDIA NIM · Groq · OpenRouter
   Cerebras · Gemini · Mistral · DeepSeek · GitHub Models · OpenCode Zen
   và mọi API tương thích OpenAI
```

## Tính năng

- **Dùng được ngay với model `auto`.** Thêm API key của một provider là gọi được, không cần cấu hình định tuyến. `auto` thử lần lượt mọi provider đang có key, lỗi thì chuyển tiếp.
- **Ba chuẩn API cho chat:** OpenAI Chat Completions, OpenAI Responses và Anthropic Messages, có stream, tool calling, ảnh đầu vào và suy luận (reasoning).
- **Embeddings và tạo ảnh** (`/v1/embeddings`, `/v1/images/generations`) dùng chung cơ chế chọn nguồn và tự chuyển khi lỗi. Ảnh trả về cả `url` lẫn `b64_json`.
- **ChatGPT bằng tài khoản Plus/Pro:** đăng nhập bằng mã thiết bị hoặc dán `auth.json` của Codex CLI. Token được tự làm mới và mã hóa khi lưu.
- **Combo (alias):** đặt một tên model ảo (vd `gpt-4o`) trỏ tới nhiều model thật, với bốn chiến lược: ưu tiên, xoay vòng, nhanh nhất, ngẫu nhiên.
- **Chịu lỗi tốt:** nhiều key cho mỗi provider, tạm khóa key bị 429/hết hạn mức đến lúc reset, circuit breaker cho nguồn lỗi liên tiếp, chỉ đổi nguồn khi chưa gửi chữ nào cho client.
- **Bảo mật:** API key riêng cho từng bot (giới hạn model, giới hạn request/phút), token và key provider mã hóa bằng `SECRET_KEY`, trang quản trị chống dò mật khẩu và CSRF.
- **Trang quản trị** sáng/tối: thống kê lưu lượng, lỗi, token theo model và theo key; quản lý provider, combo, key; khu thử nghiệm chat, tạo ảnh, embedding; sao lưu dữ liệu một chạm.
- **RTK Token Saver:** nén output tool dài (git diff, grep, log lặp) trước khi gửi lên model, giảm token cho các agent lập trình.

## Cài đặt nhanh

Cần Python 3.11 trở lên.

```bash
git clone https://github.com/traique/unirouter && cd unirouter
pip install -r requirements.txt
cp .env.example .env        # đặt ADMIN_PASSWORD và SECRET_KEY
python main.py              # mở http://localhost:20128/admin
```

App tự đọc file `.env` trong thư mục chạy. Tạo `SECRET_KEY` bằng:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Hoặc dùng Docker:

```bash
docker build -t unirouter .
docker run -d -p 20128:20128 -v $PWD/data:/app/data \
  -e ADMIN_PASSWORD=... -e SECRET_KEY=... unirouter
```

### Biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `ADMIN_PASSWORD` | Mật khẩu trang `/admin`. Bỏ trống thì trang quản trị bị tắt. |
| `SECRET_KEY` | Khóa mã hóa token/API key trong SQLite và ký cookie. **Không đổi sau khi đã thêm tài khoản.** Bỏ trống thì app tự sinh và lưu ở `data/secret.key`. |
| `ADMIN_USERNAME` | Tên đăng nhập, mặc định `admin`. |
| `MASTER_API_KEY` | Tùy chọn: một key gọi được mọi model. Key cho từng bot nên tạo trong `/admin`. |
| `PORT` / `SERVER_PORT` | Cổng lắng nghe, mặc định `20128`. |
| `PUBLIC_URL` | Địa chỉ public (vd `https://ai.example.com`) dùng khi tạo link ảnh. Bỏ trống thì lấy từ request. |
| `DATA_DIR` | Thư mục dữ liệu, mặc định `./data`. |
| `COOKIE_SECURE` | `auto` / `true` / `false`: cookie admin chỉ gửi qua HTTPS. |
| `REQUEST_TIMEOUT` | Thời gian chờ upstream tối đa (giây), mặc định 300. |

## Triển khai trên hosting kiểu panel (Botkeep, Pterodactyl…)

1. Tạo server Python, 1 slot, khoảng 256 MB RAM là đủ.
2. Đưa code lên bằng GitHub hoặc file ZIP (giải nén sao cho `main.py` và `requirements.txt` nằm ở thư mục gốc).
3. Lệnh khởi động: `python main.py`. Nếu panel có ô "requirements file", điền `requirements.txt`.
4. Thêm biến `ADMIN_PASSWORD` và `SECRET_KEY` trong mục biến môi trường của panel. Panel ghi biến vào `.env` hay truyền thẳng vào tiến trình đều được.
5. App tự dùng cổng panel cấp qua `SERVER_PORT`. Kiểm tra bằng `http://<địa-chỉ>:<cổng>/health`, rồi mở `/admin`.
6. Nên gắn domain có HTTPS nếu panel hỗ trợ, và đặt `PUBLIC_URL` theo domain đó.

## Bắt đầu trong trang quản trị

1. **Providers:** chọn một provider trong mục *Thêm nhanh*, bấm **Lấy key** để đăng ký (đa số miễn phí), dán key vào. App tự lấy danh sách model và gọi thử. Có thể dán nhiều key, mỗi dòng một key, để xoay vòng.
2. **ChatGPT:** bấm **Đăng nhập ChatGPT**, mở link và nhập mã. Nếu máy chủ bị chặn khi đăng nhập, chạy `codex login` trên máy bạn rồi dán nội dung `~/.codex/auth.json` vào **Nhập auth.json**.
3. **API Keys:** tạo một key `sk-uni-…` cho mỗi bot hoặc công cụ.
4. Gọi model `auto`. Phần **Cài đặt → Kết nối client** có sẵn cấu hình mẫu để copy.

Bấm vào một chip model trong thẻ provider để chọn nhanh: đặt làm model cho `auto`, gọi thử, copy tên hoặc bỏ khỏi danh sách. Chip có nhãn `auto` là model provider đó đang dùng khi gọi `auto`.

Provider không có trong danh sách: bấm **Provider tùy chỉnh**, nhập tên, Base URL, API key. Header và các tùy chọn tương thích được tự xử lý.

## Gọi API

```bash
curl https://ai.example.com/v1/chat/completions \
  -H "Authorization: Bearer sk-uni-..." -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Xin chào"}],"stream":true}'
```

| Client | Cấu hình |
|---|---|
| OpenAI SDK, bot, n8n, Dify | `base_url = https://ai.example.com/v1`, `api_key = sk-uni-…`, `model = auto` |
| Claude Code | `ANTHROPIC_BASE_URL=https://ai.example.com`, `ANTHROPIC_AUTH_TOKEN=sk-uni-…`, `ANTHROPIC_MODEL=auto` |
| Codex CLI | trong `~/.codex/config.toml`: provider với `base_url = "https://ai.example.com/v1"`, `wire_api = "responses"` |
| Anthropic SDK | `base_url = "https://ai.example.com"`, `api_key = "sk-uni-…"` |

### Tên model

| Cách gọi | Ví dụ | Nghĩa |
|---|---|---|
| `auto` | `auto` | Mọi provider đang có key, thử lần lượt |
| `tiền_tố/model` | `cx/gpt-5.6-sol`, `groq/openai/gpt-oss-120b` | Đúng model đó của đúng provider |
| tên combo | `gpt-4o`, `fast` | Danh sách model bạn tự định nghĩa |
| tên model trần | `deepseek-chat` | Provider nào có model đó |

Với ChatGPT, thêm hậu tố `-low`, `-medium`, `-high`, `-xhigh` để chọn mức suy luận, ví dụ `cx/gpt-5.6-sol-high`. Client gửi tên model không tồn tại (vd `gpt-4o`, `claude-…`) sẽ tự chạy qua model mặc định, trừ khi bạn tắt trong Cài đặt.

### Embeddings

```bash
curl https://ai.example.com/v1/embeddings -H "Authorization: Bearer sk-uni-..." \
  -H "Content-Type: application/json" -d '{"model":"auto","input":["Xin chào","Tạm biệt"]}'
```

`auto` dùng các provider có model embedding: NVIDIA NIM, Gemini, Mistral và GitHub Models có sẵn model mặc định. Đổi model ở **Providers → Sửa → Nâng cao → Model embedding**. Lưu ý: vector từ các model khác nhau không so sánh được với nhau, nên với cơ sở dữ liệu vector hãy gọi một model cố định (vd `gm/gemini-embedding-001`) thay vì `auto`.

### Tạo ảnh

```bash
curl https://ai.example.com/v1/images/generations -H "Authorization: Bearer sk-uni-..." \
  -H "Content-Type: application/json" -d '{"model":"auto","prompt":"một chú mèo cam","size":"1024x1024"}'
```

- Provider có endpoint `/images/generations` tương thích OpenAI: đặt **Model tạo ảnh** khi sửa provider, hoặc app tự nhận diện từ tên model (flux, imagen, gpt-image…).
- ChatGPT (thử nghiệm): tạo ảnh bằng công cụ tạo ảnh của tài khoản ChatGPT, gọi qua `cx/gpt-image` hoặc để `auto` dùng khi không còn nguồn nào khác.
- Kết quả có `data[].url` (link tạm, tự xóa sau 24 giờ) và `data[].b64_json`. Gửi `"response_format":"b64_json"` nếu chỉ cần base64.

### Endpoint

| Endpoint | Mô tả |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses (chuyển thẳng tới ChatGPT, tự chuyển đổi với provider khác) |
| `POST /v1/messages`, `/v1/messages/count_tokens` | Anthropic Messages |
| `POST /v1/embeddings` | Embeddings |
| `POST /v1/images/generations` | Tạo ảnh |
| `GET /v1/models` | Danh sách model, combo và `auto` |
| `GET /health` | Kiểm tra trạng thái |

## Khi có lỗi

| Lỗi từ provider | UniRouter xử lý |
|---|---|
| 429, 401, 403, hết hạn mức | Tạm khóa key đó (theo Retry-After hoặc thời điểm reset), thử key khác cùng provider, rồi tới nguồn kế tiếp |
| 5xx, timeout, lỗi mạng | Thử nguồn kế tiếp. Lỗi liên tiếp vượt ngưỡng thì nguồn đó nghỉ một lúc |
| Model không tồn tại, vượt context | Thử nguồn kế tiếp |
| ChatGPT báo model không còn hỗ trợ (`model is not supported`) | Bỏ model đó khỏi danh sách của provider; `auto` thử ngay model ChatGPT kế tiếp |
| Request sai tham số | Trả lỗi ngay cho client |
| Lỗi giữa lúc đang stream | Báo lỗi trong stream, không ghép câu trả lời từ hai model |

Khi model suy nghĩ lâu, UniRouter gửi `: keepalive` mỗi 15 giây để proxy không cắt kết nối.

## Sao lưu và khôi phục

Toàn bộ dữ liệu nằm trong `data/unirouter.db` (cộng `data/secret.key` nếu không đặt `SECRET_KEY`). Tải bản sao ở **Cài đặt → Sao lưu**. Để khôi phục, dừng app, chép file vào `data/unirouter.db` rồi chạy lại với **cùng `SECRET_KEY`**.

## Lưu ý về ChatGPT

Provider ChatGPT dùng endpoint nội bộ mà Codex CLI dùng, **không phải API chính thức**:

- Có thể ngừng chạy khi OpenAI thay đổi backend. Khi đó cập nhật `CODEX_VERSION` trong `unirouter/providers/codex_auth.py`.
- Dùng tài khoản qua máy chủ trung gian, chia sẻ cho nhiều người hoặc gọi với tần suất cao có thể vi phạm điều khoản và khiến tài khoản bị khóa.
- Nên luôn có thêm ít nhất một provider dùng API key thật để `auto` có chỗ chuyển sang.

## Phát triển

```bash
pip install pytest openai anthropic
python -m pytest tests -q
```

Bộ test chạy với upstream giả lập và SDK `openai`/`anthropic` thật.

```
unirouter/
  api.py           endpoint /v1
  router.py        chọn nguồn, combo, tự chuyển khi lỗi
  health.py        tạm khóa key, circuit breaker, giới hạn request
  providers/       ChatGPT (Codex) và API tương thích OpenAI, danh mục provider
  formats/         chuyển đổi Chat ↔ Responses ↔ Anthropic
  rtk.py           nén output tool
  images.py        lưu tạm ảnh đã tạo
  db.py            SQLite
  admin.py         API trang quản trị
  static/admin.html
```
