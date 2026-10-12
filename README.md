# UniRouter

Cổng AI tự host: gom ChatGPT và các nhà cung cấp model (NVIDIA NIM, Groq, OpenRouter, Gemini, Mistral, GitHub Models…) về **một địa chỉ, một API key**. Bot, Claude Code, Codex CLI hay bất kỳ app nào dùng OpenAI SDK đều gọi được, và khi một nguồn lỗi hoặc hết hạn mức thì UniRouter tự chuyển sang nguồn khác.

Viết bằng Python (FastAPI), một tiến trình, dữ liệu trong một file SQLite. Chạy thoải mái trên VPS nhỏ hoặc gói hosting miễn phí: khoảng 70–90 MB RAM.

```
 Bot · Claude Code · Codex CLI · Cursor · n8n · app OpenAI SDK
                         │
      Chat Completions · Responses · Anthropic Messages
      Embeddings · Tạo ảnh · Giọng nói ↔ chữ
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
   Microsoft Azure Speech · Microsoft Edge TTS (giọng đọc tiếng Việt)
   và mọi API tương thích OpenAI
```

## Tính năng

- **Dùng được ngay với model `auto`.** Thêm API key của một provider là gọi được, không cần cấu hình định tuyến. `auto` thử lần lượt mọi provider đang có key, lỗi thì chuyển tiếp.
- **Ba chuẩn API cho chat:** OpenAI Chat Completions, OpenAI Responses và Anthropic Messages, có stream, tool calling, ảnh đầu vào và suy luận (reasoning).
- **Embeddings và tạo ảnh** (`/v1/embeddings`, `/v1/images/generations`) dùng chung cơ chế chọn nguồn và tự chuyển khi lỗi. Ảnh trả về cả `url` lẫn `b64_json`.
- **Giọng nói:** chuyển giọng nói thành chữ (`/v1/audio/transcriptions`, Whisper miễn phí qua Groq) và chữ thành giọng nói (`/v1/audio/speech`, giọng Neural tiếng Việt của Microsoft qua Edge TTS không cần key hoặc Azure Speech). Bot nhận tin nhắn thoại và trả lời bằng giọng nói được.
- **ChatGPT bằng tài khoản Plus/Pro:** đăng nhập bằng mã thiết bị hoặc dán `auth.json` của Codex CLI. Token được tự làm mới và mã hóa khi lưu.
- **Combo (alias):** đặt một tên model ảo (vd `gpt-4o`) trỏ tới nhiều model thật, với bốn chiến lược: ưu tiên, xoay vòng, nhanh nhất, ngẫu nhiên.
- **Chịu lỗi tốt:** nhiều key cho mỗi provider, tạm khóa key bị 429/hết hạn mức đến lúc reset, circuit breaker cho nguồn lỗi liên tiếp, chỉ đổi nguồn khi chưa gửi chữ nào cho client.
- **Bảo mật:** API key riêng cho từng bot (giới hạn model, giới hạn request/phút), token và key provider mã hóa bằng `SECRET_KEY`, trang quản trị chống dò mật khẩu và CSRF.
- **Trang quản trị** sáng/tối: thống kê lưu lượng, lỗi, token theo model và theo key; quản lý provider, combo, key; khu thử nghiệm chat, tạo ảnh, embedding, đọc văn bản và ghi âm chuyển thành chữ; sao lưu dữ liệu một chạm.
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
| `tiền_tố/model` | `cx/gpt-5.5`, `groq/openai/gpt-oss-120b` | Đúng model đó của đúng provider |
| tên combo | `gpt-4o`, `fast` | Danh sách model bạn tự định nghĩa |
| tên model trần | `deepseek-chat` | Provider nào có model đó |

Với ChatGPT, thêm hậu tố `-low`, `-medium`, `-high`, `-xhigh` để chọn mức suy luận, ví dụ `cx/gpt-5.5-high`. Client gửi tên model không tồn tại (vd `gpt-4o`, `claude-…`) sẽ tự chạy qua model mặc định, trừ khi bạn tắt trong Cài đặt.

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

### Giọng nói → chữ

```bash
curl https://ai.example.com/v1/audio/transcriptions -H "Authorization: Bearer sk-uni-..." \
  -F file=@tin-nhan-thoai.ogg -F model=auto -F language=vi
# {"text": "..."}
```

- Thêm key **Groq** là dùng được ngay: mặc định `whisper-large-v3-turbo`, miễn phí, nhận mp3, wav, m4a, ogg, webm… tối đa 25 MB. Mistral (`voxtral-mini-latest`) cũng có sẵn.
- Hỗ trợ các tham số chuẩn OpenAI: `language`, `prompt`, `response_format` (`json`, `text`, `srt`, `vtt`, `verbose_json`), `temperature`. SDK gửi `model="whisper-1"` vẫn chạy qua `auto`.

### Chữ → giọng nói

```bash
curl https://ai.example.com/v1/audio/speech -H "Authorization: Bearer sk-uni-..." \
  -H "Content-Type: application/json" -o tra-loi.mp3 \
  -d '{"model":"auto","input":"Xin chào, mình là Lan Anh","voice":"vi-VN-HoaiMyNeural","speed":1.1}'
```

Có hai nguồn giọng đọc Microsoft, cùng bộ giọng Neural. `auto` dùng Azure trước, Edge sau.

**Microsoft Edge TTS (không cần key).** Vào Providers → *Microsoft Edge TTS* → **Bật**. App dùng thư viện [edge-tts](https://github.com/rany2/edge-tts) để gọi dịch vụ "Đọc to" của trình duyệt Edge. Đây không phải API chính thức: Microsoft có thể chặn hoặc đổi bất cứ lúc nào, và dùng ngoài trình duyệt Edge có thể trái điều khoản của họ. Edge TTS luôn trả MP3.

**Microsoft Azure Speech (API chính thức, gói miễn phí):**

1. Tạo tài khoản Azure, vào **Create → Speech service**, chọn gói **Free F0** (khoảng 500.000 ký tự mỗi tháng) và Region gần bạn, ví dụ *Southeast Asia*.
2. Mở tài nguyên vừa tạo → **Keys and Endpoint**, copy **Key 1**.
3. Trong `/admin` → Providers → *Microsoft Azure Speech* → **Thêm key**: chọn đúng Region, dán key. App tự đọc thử một câu để kiểm tra.

Giọng tiếng Việt: `vi-VN-HoaiMyNeural` (nữ), `vi-VN-NamMinhNeural` (nam). Bấm **Sửa → Lấy danh sách model** để thấy thêm các giọng *Multilingual* đọc được nhiều thứ tiếng. Trường `voice` nhận tên giọng Microsoft; giọng của OpenAI như `alloy` được đổi sang giọng mặc định. `response_format`: `mp3` (mặc định), `opus`, `wav`, `pcm`. Provider tương thích OpenAI có `/audio/speech` cũng dùng được: đặt *Model chữ → giọng* khi sửa provider.

### Endpoint

| Endpoint | Mô tả |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses (chuyển thẳng tới ChatGPT, tự chuyển đổi với provider khác) |
| `POST /v1/messages`, `/v1/messages/count_tokens` | Anthropic Messages |
| `POST /v1/embeddings` | Embeddings |
| `POST /v1/images/generations` | Tạo ảnh |
| `POST /v1/audio/transcriptions` | Giọng nói → chữ |
| `POST /v1/audio/speech` | Chữ → giọng nói |
| `GET /v1/models` | Danh sách model, combo và `auto` |
| `GET /health` | Kiểm tra trạng thái |

## Khi có lỗi

| Lỗi từ provider | UniRouter xử lý |
|---|---|
| 429, 401, 403, hết hạn mức | Tạm khóa key đó (theo Retry-After hoặc thời điểm reset), thử key khác cùng provider, rồi tới nguồn kế tiếp |
| 5xx, timeout, lỗi mạng | Thử nguồn kế tiếp. Lỗi liên tiếp vượt ngưỡng thì nguồn đó nghỉ một lúc |
| Model không tồn tại, vượt context | Thử nguồn kế tiếp |
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
  providers/       ChatGPT (Codex), API tương thích OpenAI, Azure Speech, danh mục provider
  formats/         chuyển đổi Chat ↔ Responses ↔ Anthropic
  rtk.py           nén output tool
  images.py        lưu tạm ảnh đã tạo
  db.py            SQLite
  admin.py         API trang quản trị
  static/admin.html
```
