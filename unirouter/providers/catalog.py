"""Danh sách provider dựng sẵn. Admin có thể sửa model, base URL hoặc thêm provider mới.

Danh sách model chỉ là gợi ý ban đầu (10/2026).
Dùng nút "Lấy danh sách model" trong /admin để cập nhật từ chính provider.
"""
from __future__ import annotations

from typing import Any

CODEX_LITE_MODELS = ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"]

PRESETS: list[dict[str, Any]] = [
    {
        "id": "codex", "signup_url": "https://chatgpt.com", "alias": "cx", "name": "ChatGPT (Codex OAuth)", "type": "codex",
        "base_url": "https://chatgpt.com/backend-api/codex/responses",
        "models": ["gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"],
        "options": {"lite_models": CODEX_LITE_MODELS, "default_effort": "low"},
        "note": "Đăng nhập bằng tài khoản ChatGPT (Plus/Pro). Dùng endpoint nội bộ của Codex, "
                "không phải API chính thức: có thể hỏng khi OpenAI đổi backend và có rủi ro khóa tài khoản.",
    },
    {
        "id": "nvidia", "signup_url": "https://build.nvidia.com/settings/api-keys", "free": True, "alias": "nim", "name": "NVIDIA NIM", "type": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": ["nvidia/nemotron-3-ultra-550b-a55b", "deepseek-ai/deepseek-v4-flash",
                    "deepseek-ai/deepseek-v4-pro", "moonshotai/kimi-k2.6", "z-ai/glm-5.2", "minimaxai/minimax-m3"],
        "options": {"embedding_model": "nvidia/nv-embedqa-e5-v5"},
        "note": "Key miễn phí tại build.nvidia.com",
    },
    {
        "id": "groq", "signup_url": "https://console.groq.com/keys", "free": True, "alias": "groq", "name": "Groq", "type": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"],
        "options": {"stt_model": "whisper-large-v3-turbo"},
        "note": "Key miễn phí tại console.groq.com",
    },
    {
        "id": "openrouter", "signup_url": "https://openrouter.ai/settings/keys", "free": True, "alias": "or", "name": "OpenRouter", "type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["nvidia/nemotron-3-super-120b-a12b:free", "google/gemma-4-26b-a4b-it:free"],
        "headers": {"HTTP-Referer": "https://github.com/unirouter", "X-Title": "UniRouter"},
        "note": "Các model có hậu tố :free không tốn phí",
    },
    {
        "id": "tokenrouter", "signup_url": "https://tokenrouter.io", "free": True, "alias": "tr", "name": "TokenRouter", "type": "openai",
        "base_url": "https://api.tokenrouter.io/v1",
        "models": ["qwen/qwen3.8-max-free"],
    },
    {
        "id": "cerebras", "signup_url": "https://cloud.cerebras.ai", "free": True, "alias": "cb", "name": "Cerebras", "type": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "models": ["gpt-oss-120b", "zai-glm-4.7", "qwen-3-235b-a22b-instruct-2507"],
    },
    {
        "id": "gemini", "signup_url": "https://aistudio.google.com/apikey", "free": True, "alias": "gm", "name": "Google Gemini", "type": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
        "options": {"stream_usage": False, "embedding_model": "gemini-embedding-001"},
        "note": "Key tại aistudio.google.com (endpoint tương thích OpenAI)",
    },
    {
        "id": "mistral", "signup_url": "https://console.mistral.ai/api-keys", "free": True, "alias": "ms", "name": "Mistral", "type": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-medium-latest", "codestral-latest"],
        "options": {"stream_usage": False, "embedding_model": "mistral-embed", "stt_model": "voxtral-mini-latest"},
    },
    {
        "id": "deepseek", "signup_url": "https://platform.deepseek.com/api_keys", "alias": "ds", "name": "DeepSeek", "type": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "opencode-zen", "alias": "ocz", "name": "OpenCode Zen", "type": "openai",
        "base_url": "https://opencode.ai/zen/v1", "models": [],
        "signup_url": "https://opencode.ai/auth", "free": True,
        "note": "API key chính chủ tại opencode.ai/auth. Có cả model miễn phí (đuôi -free) và trả phí; "
                "dán key xong app tự lấy danh sách model, model miễn phí xếp trước.",
    },
    {
        "id": "github", "alias": "gh", "name": "GitHub Models", "type": "openai",
        "base_url": "https://models.github.ai/inference", "models": [],
        "options": {"models_url": "https://models.github.ai/catalog/models",
                    "embedding_model": "openai/text-embedding-3-small"},
        "signup_url": "https://github.com/settings/personal-access-tokens/new", "free": True,
        "note": "Miễn phí với tài khoản GitHub (có giới hạn theo ngày). Tạo token có quyền \"Models: read\" rồi dán vào.",
    },
    {
        "id": "azure-speech", "alias": "az", "name": "Microsoft Azure Speech", "type": "azure",
        "base_url": "https://southeastasia.tts.speech.microsoft.com",
        "models": ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
        "options": {"region": "southeastasia"},
        "signup_url": "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices", "free": True,
        "note": "Giọng đọc Neural của Microsoft, có tiếng Việt (HoaiMy giọng nữ, NamMinh giọng nam). Tạo tài nguyên "
                "Speech gói F0 miễn phí (khoảng 500.000 ký tự/tháng), dán Key 1 và chọn đúng Region.",
    },
    {
        "id": "edge-tts", "alias": "edge", "name": "Microsoft Edge TTS", "type": "edge",
        "base_url": "https://speech.platform.bing.com", "models": ["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"],
        "free": True,
        "note": "Giọng đọc của tính năng Đọc to trên Edge, không cần key (thư viện edge-tts). Không phải API chính "
                "thức: Microsoft có thể chặn bất cứ lúc nào, nên bật kèm Azure Speech để dự phòng.",
    },
]
