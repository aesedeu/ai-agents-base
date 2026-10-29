# Альтернатива vLLM: та же модель через oMLX

[oMLX](https://github.com/jundot/omlx) — сервер LLM для Apple Silicon на MLX: несколько моделей в
одном процессе (загружает по запросу, выгружает самые старые при нехватке памяти), KV-кэш на SSD,
веб-админка. API — OpenAI-совместимый, как у vLLM.

План: останавливаем vLLM и запускаем oMLX **на том же порту 8080, с тем же ключом и тем же именем
модели** `qwen3.5-9b`. Тогда в кластере ничего не меняется: Service `local-llm:8080` и модель
`local-llm` в LiteLLM (часть 3 README) работают с oMLX как есть.

| | vLLM (`run_vllm_mac.sh`) | oMLX |
|---|---|---|
| моделей в процессе | одна | несколько, по запросу |
| настройки модели (имя, «думать») | флаги запуска | админка или API, хранятся в `model_settings.json` |
| метрики Prometheus (`/metrics`) | есть → Grafana (часть 6) | нет → смотрим в mlxtop и админке oMLX |
| работает на | Linux + NVIDIA, Mac (vllm-metal) | только Mac |

---

## Шаг 1. Установка (один раз)

```bash
brew install jundot/omlx/omlx        # CLI-сервер; или приложение oMLX.app — у него тот же CLI: ~/.omlx/bin/omlx
omlx --version
```

## Шаг 2. Освободить порт 8080

Остановите vLLM — **Ctrl-C** в его терминале (часть 3, шаг 3.2). Проверка — порт свободен:

```bash
lsof -iTCP:8080 -sTCP:LISTEN          # пусто
```

## Шаг 3. Запуск oMLX — в отдельном терминале

```bash
cd lesson5_litellm_langfuse
set -a; . ./.env; set +a                                  # VLLM_API_KEY — тот же ключ, что у vLLM
export HF_HUB_CACHE=/Volumes/aweeu_t7/hf_cache/hub       # где лежат веса (как для vLLM)

omlx serve \
  --base-path ~/.omlx-lesson5 \
  --host 0.0.0.0 \
  --port 8080 \
  --api-key "$VLLM_API_KEY" \
  --max-concurrent-requests 8 \
  --memory-guard-gb 16
```

| Флаг | Зачем |
|---|---|
| `--base-path ~/.omlx-lesson5` | отдельная папка для настроек, логов и кэша. `serve` **записывает** туда `settings.json`, и с папкой по умолчанию (`~/.omlx`) он перезаписал бы настройки вашего oMLX.app |
| `--host 0.0.0.0 --port 8080` | слушать все интерфейсы на том же порту, что и vLLM: к модели придут из кластера |
| `--api-key` | без ключа — `401`; этим же ключом входят в админку |
| `--max-concurrent-requests 8` | сколько запросов одновременно (аналог `--max-num-seqs` у vLLM) |
| `--memory-guard-gb 16` | потолок памяти для oMLX, ≈ как `--gpu-memory-utilization 0.3` у vLLM: рядом работает minikube |

Модели oMLX находит сам — в кэше Hugging Face (`HF_HUB_CACHE`) и в `--model-dir`. В логе запуска:

```
Discovered model: lmstudio-community--Qwen3.5-9B-MLX-4bit (type: vlm, engine: vlm, size: 5.82GB, text-only: 4.93GB)
```

Веса загружаются в память при **первом запросе** к модели (несколько секунд), а не при старте.

## Шаг 4. Настройки модели: имя `qwen3.5-9b` и без рассуждений

В oMLX модель называется по папке: `lmstudio-community--Qwen3.5-9B-MLX-4bit`, и Qwen3.5 по умолчанию
**думает** перед ответом (сотни токенов на «Столица Чехии?»). Делаем как у vLLM: короткое имя
(alias) и без рассуждений.

**Через админку:** http://localhost:8080/admin → вход по API-ключу (`VLLM_API_KEY`) → **Settings** →
**Model Settings** → ⚙ в строке `lmstudio-community/Qwen3.5-9B-MLX-4bit` → *Model Alias* = `qwen3.5-9b`,
*Enable Thinking* — выключен → сохранить.

**Через API** (то же самое; админка работает по сессии — сначала логин, cookie в файл):

```bash
curl -s -c ~/.omlx-lesson5/admin.cookies localhost:8080/admin/api/login \
  -H "Content-Type: application/json" -d "{\"api_key\": \"$VLLM_API_KEY\"}"            # {"success":true}

curl -s -b ~/.omlx-lesson5/admin.cookies -X PUT localhost:8080/admin/api/models/lmstudio-community--Qwen3.5-9B-MLX-4bit/settings \
  -H "Content-Type: application/json" -d '{"model_alias": "qwen3.5-9b", "enable_thinking": false}'
```

Настройки сохраняются в `~/.omlx-lesson5/model_settings.json` и переживают перезапуск oMLX.

## Шаг 5. Проверка — те же запросы, что для vLLM (шаг 3.3 README)

```bash
curl -s localhost:8080/v1/models                         # без ключа → 401
curl -s localhost:8080/v1/models -H "Authorization: Bearer $VLLM_API_KEY"     # первой — qwen3.5-9b

curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5-9b", "messages": [{"role": "user", "content": "Столица Чехии? Одним словом."}]}'
# → "Прага", reasoning_content пустой, completion_tokens ≈ 2

curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" -d '{
    "model": "qwen3.5-9b",
    "messages": [{"role": "user", "content": "Посчитай платёж по кредиту 150000 на 24 месяца"}],
    "tools": [{"type": "function", "function": {"name": "loan_payment", "description": "Monthly annuity payment",
      "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "months": {"type": "integer"}},
                     "required": ["amount", "months"]}}}]
  }'
# → tool_calls: loan_payment {"amount": 150000, "months": 24}
```

> В `/v1/models` видны **все** модели из кэша Hugging Face, а не только наша. Лишние можно скрыть
> переключателем **API** в той же таблице Model Settings (или запускать с `--no-hf-cache` и папкой
> только с нужными моделями в `--model-dir`).

## Шаг 6. Кластер — без изменений

Адрес (`local-llm:8080` → `192.168.105.1:8080`), ключ и имя модели те же, поэтому LiteLLM и агенты
просто начинают ходить в oMLX:

```bash
kubectl run t --rm -it --restart=Never --image=curlimages/curl:8.11.1 -- \
  -s -H "Authorization: Bearer $VLLM_API_KEY" http://local-llm:8080/v1/models      # модели oMLX — из кластера

uv run python images/agents/ask.py http://risk.agents.test "Можно ли выдать счёту 2378 кредит 150 000 CZK на 24 месяца?"
```

## Шаг 7. Наблюдение: mlxtop, лог и админка oMLX

У oMLX нет `/metrics` для Prometheus, поэтому дашборд vLLM в Grafana (часть 6) для него не
используется. Нагрузку смотрим локально.

**mlxtop.** Адрес и ключ oMLX mlxtop читает из `~/.config/omlx-coding/server.env` (без ключа он видит
только `/health`: модели есть, а скорости и очереди — нет):

```bash
mkdir -p ~/.config/omlx-coding
printf 'PORT=8080\nAPI_KEY=%s\n' "$VLLM_API_KEY" > ~/.config/omlx-coding/server.env
chmod 600 ~/.config/omlx-coding/server.env                # в файле ключ — читать может только владелец

MLXTOP_PROVIDER=omlx mlxtop          # GEN / PREFILL tok/s, активные запросы, память модели, своп Mac
```

Проверка подключения — `MLXTOP_PROVIDER=omlx mlxtop doctor`: должно быть `oMLX · Connected` и
`/admin/api/stats?scope=session: responded`.

**Лог oMLX** — строка на каждый запрос (в терминале, где запущен `omlx serve`):

```
Chat completion: model=lmstudio-community--Qwen3.5-9B-MLX-4bit, 461 tokens in 13.92s (33.1 tok/s), prompt: 2056, …
```

**Админка** http://localhost:8080/admin → **Dashboard**: загруженные модели, запросы, токены/с,
память, попадания в кэш.

## Шаг 8. Вернуться на vLLM

**Ctrl-C** в терминале oMLX, затем снова `./part_3_own_model/run_vllm_mac.sh` (шаг 3.2 README).
Пока порт пуст, агенты работают через fallback на OpenRouter (шаг 3.8).
