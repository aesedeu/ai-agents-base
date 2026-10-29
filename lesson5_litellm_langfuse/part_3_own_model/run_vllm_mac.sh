#!/bin/bash
# Своя модель на ноутбуке (Apple Silicon): vLLM + плагин vllm-metal (считает на GPU Mac через MLX).
# Запускать НА MAC, в отдельном терминале — сервер работает, пока открыт терминал (Ctrl-C — остановить).
#
# Один раз: окружение vllm-metal  (https://github.com/vllm-project/vllm-metal)
#   curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh | bash
set -e
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a                             # VLLM_API_KEY — ключ к нашей модели
: "${VLLM_API_KEY:?впишите VLLM_API_KEY в .env}"

source ~/.venv-vllm-metal/bin/activate
export VLLM_METAL_BACKEND=mlx                        # считать на MLX (GPU Mac через Metal) — это и так по умолчанию
export HF_HUB_CACHE=${HF_HUB_CACHE:-$HOME/.cache/huggingface/hub}   # где лежат веса (скачаются при первом запуске, ~6 ГБ)

MODEL=lmstudio-community/Qwen3.5-9B-MLX-4bit         # Qwen3.5 9B, 4-битная квантизация под MLX

args=(
  --model "$MODEL"                                   # какие веса грузить (репозиторий Hugging Face)
  --served-model-name qwen3.5-9b                     # имя модели в API — его укажем в LiteLLM
  --host 0.0.0.0                                     # слушать все интерфейсы: к модели придут из кластера minikube
  --port 8080                                        # порт API (OpenAI-совместимый: /v1/chat/completions, /v1/models)
  --api-key "$VLLM_API_KEY"                          # без ключа — 401 (порт виден всей локальной сети)
  --max-model-len 65536                              # максимум токенов в одном запросе (промпт + ответ); меньше — меньше памяти под KV-кэш
  --gpu-memory-utilization 0.5                       # доля памяти GPU под vLLM (веса + KV-кэш): 0.3 × 48 ГБ ≈ 15 ГБ.
                                                     #   По умолчанию 0.92 — vLLM займёт почти всю память Mac, и вместе с minikube начнётся своп
  --max-num-seqs 8                                   # сколько запросов модель обрабатывает одновременно (у нас 2 аналитика + 2 риск-менеджера)
  --max-num-batched-tokens 8192                      # сколько токенов максимум в одном шаге батча (ограничивает пики памяти)
  --enable-chunked-prefill                           # длинный промпт считать кусками, не блокируя генерацию у других запросов
  --enable-prefix-caching                            # переиспользовать KV-кэш общего начала промптов (системный промпт агента + история)
  --language-model-only                              # Qwen3.5 мультимодальная — картинки нам не нужны, не грузим vision-часть
  --reasoning-parser qwen3                           # «мысли» модели (<think>…</think>) отдавать отдельно — в поле reasoning_content
  --default-chat-template-kwargs '{"enable_thinking": false}'   # по умолчанию НЕ думать: агентам важна скорость
                                                     #   (включить на один запрос: "chat_template_kwargs": {"enable_thinking": true})
  --enable-auto-tool-choice                          # модель сама решает, вызывать ли инструмент (tool_choice: auto) — без этого агенты не работают
  --tool-call-parser qwen3_coder                     # как вытаскивать вызовы инструментов из текста модели (формат Qwen3.5)
  --uvicorn-log-level warning                        # не печатать каждую строку веб-сервера
)
echo "🚀 vLLM: $MODEL → http://0.0.0.0:8080 (модель в API: qwen3.5-9b)"
exec python -m vllm.entrypoints.openai.api_server "${args[@]}"
