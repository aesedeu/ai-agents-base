# Занятие 5 — LLM-шлюз LiteLLM, своя модель и Langfuse в Kubernetes

Продолжаем занятие 4. Стартуем с **уже поднятой** мультиагентной системы (база, MCP-сервер,
аналитик, риск-менеджер с общей памятью) и достраиваем вокруг неё инфраструктуру LLM:

- **LiteLLM** — LLM-шлюз (gateway): один OpenAI-совместимый адрес для всех агентов, свои ключи
  у каждого агента, лимиты и бюджеты, учёт расходов, маршрутизация между моделями;
- **своя модель** — Qwen3.5 9B в vLLM на GPU ноутбука, подключённая к шлюзу как ещё одна модель,
  с запасным путём (fallback) на OpenRouter;
- **Langfuse** внутри кластера — все вызовы LLM видны в трейсах, данные не уходят наружу;
- **мониторинг** — Prometheus + Grafana с дашбордом vLLM: очередь, KV-кэш, скорость генерации.

```
 ноутбук ──── Ingress ──┬── risk.agents.test ──▶ ⚖️ risk-manager ×2 ── A2A ──▶ 📊 analyst ×2 ── MCP ──▶ 🔧 bank-mcp ×2
                        │                              │                             │                          │
                        │                              └────────── LLM ──────────────┤                     🗄 bank-db
                        │                                                            ▼                   (Postgres: bank,
                        ├── llm.agents.test ───────────────────────────▶ 🚦 LiteLLM ×2 ── Redis           agent, litellm)
                        │                                                 │       │
                        │                                    bank-llm ◀───┘       └───▶ local-llm ──▶ 💻 vLLM на Mac
                        │                                  (OpenRouter)            (fallback → bank-llm)   Qwen3.5 9B
                        │                                                 │ трейсы                            │ метрики
                        ├── langfuse.agents.test ──────▶ 🔭 Langfuse (web, worker, Postgres, ClickHouse, Redis, S3)
                        └── grafana.agents.test ───────▶ 📈 Prometheus + Grafana ◀─────────────────────────────┘
```

Формат: **лекция + практика сразу в кластере**. Каждая часть — шаги с командами, которые
выполняются по порядку из папки `lesson5_litellm_langfuse/`.

| Часть | Тема | Что делаем |
|---|---|---|
| 0 | Старт | кластер + весь стек занятия 4 |
| 1 | **LiteLLM** | шлюз в кластере: модели, master key, база, Redis, Ingress `llm.agents.test`, админка |
| 2 | Агенты через шлюз | виртуальные ключи агентов, переключение без изменения кода и Deployment-ов |
| 3 | **Своя модель** | vLLM на Mac → Service без selector → модель в шлюзе → агенты → fallback |
| 4 | **Langfuse** | Helm: cert-manager, оператор ClickHouse, Langfuse v4, headless-инициализация |
| 5 | Шлюз → Langfuse | трейс каждого вызова LLM без строчки кода в агентах |
| 6 | **Мониторинг** | Prometheus + Grafana, метрики vLLM, дашборд 24756 |

---

## Лекция: зачем агентам шлюз

**Без шлюза** каждый агент сам ходит к провайдеру: у каждого ключ OpenRouter в Secret, адрес и
имя модели в конфиге. Вопросы «сколько потратил аналитик?», «переключить всех на другую модель»,
«ограничить экспериментального агента $5», «OpenRouter лёг — что делать?» решаются в коде каждого агента.

**Со шлюзом** между агентами и провайдерами — один сервис:

| Что даёт шлюз | Как это выглядит в LiteLLM |
|---|---|
| один адрес и один API для всех моделей | OpenAI-совместимый `/v1/chat/completions` — агенту всё равно, кто за ним |
| имена моделей — наши, а не провайдера | агент просит `bank-llm`, шлюз решает, что это `openrouter/openai/gpt-6-luna` |
| ключи провайдеров — только в шлюзе | агенты получают **виртуальные ключи**: свой у каждого, отзываются отдельно |
| лимиты и бюджеты | на ключ: модели, `max_budget`, `rpm_limit` / `tpm_limit` |
| учёт расходов | токены и $ по каждому ключу, модели, пользователю |
| надёжность | повторы, **fallback** на другую модель, «охлаждение» сломанной модели |
| свои модели рядом с облачными | vLLM, Ollama, TGI — ещё одна строка в списке моделей |
| наблюдаемость | callback в Langfuse: каждый вызов — трейс, без изменения агентов |

**Своя модель.** Облачная модель — это деньги за каждый токен и данные клиентов, уходящие наружу.
Своя модель (vLLM на своём GPU) — фиксированная цена железа и данные внутри. Шлюз позволяет
держать обе: свою — основной, облачную — запасной (fallback).

**Langfuse внутри кластера.** В занятии 3 трейсы уходили в Langfuse Cloud. В банке так нельзя:
промпты содержат данные клиентов. Поднимаем Langfuse у себя. Langfuse v4 — это не один контейнер,
а небольшая система:

| Компонент | Зачем |
|---|---|
| `langfuse-web` | UI и API (сюда шлют трейсы) |
| `langfuse-worker` | фоновая обработка: разбирает события из очереди и пишет в ClickHouse |
| Postgres | пользователи, проекты, ключи, промпты |
| ClickHouse (+ Keeper) | трейсы и наблюдения — колоночная БД для аналитики |
| Redis (Valkey) | очередь между web и worker |
| S3 (SeaweedFS) | сырые события и медиа |

ClickHouse ставится **оператором**: чарт Langfuse создаёт не Pod-ы, а свои ресурсы
`ClickHouseCluster` и `KeeperCluster` (CRD), а оператор — программа в кластере — превращает их в
StatefulSet-ы, Service-ы и конфиги. Так в Kubernetes работают почти все «тяжёлые» базы.

**Трейсы и метрики — разное.** Langfuse отвечает на вопрос «что спросили и что ответила модель в
этом запросе». Prometheus + Grafana — «как себя чувствует сервер модели»: сколько запросов в
очереди, насколько заполнен KV-кэш, сколько токенов в секунду, через сколько пришёл первый токен.

---

## Часть 0. Старт: стек занятия 4

**Нужно (macOS):** `brew install minikube kubectl helm qemu socket_vmnet` (Helm ≥ 3.17) и один раз —
сетевой демон для QEMU: `sudo brew services start socket_vmnet`.

### Шаг 0.1. Ключ OpenRouter

```bash
cp .env.example .env     # вписать OPENROUTER_API_KEY
```

### Шаг 0.2. Кластер и образ агентов

```bash
make setup               # 3 ноды minikube (4 CPU, 8 ГБ, 40 ГБ каждая) + ingress, metrics-server, local-path   ~4 мин
make images              # образ агентов bank-agents:1 на всех нодах                                          ~3 мин
make hosts               # risk/llm/langfuse/grafana/prometheus.agents.test → IP кластера (спросит пароль sudo)
```

> Ноды вдвое крупнее, чем в занятии 4: кроме агентов в кластере будут LiteLLM, Langfuse с его базами
> и мониторинг.

### Шаг 0.3. Весь стек занятия 4

```bash
make secret              # ключ OpenRouter → Secret-ы openrouter, llm-key-analyst, llm-key-risk
make base                # база + загрузка данных + MCP ×2 + аналитик ×2 + риск-менеджер ×2 + Ingress        ~2 мин
uv sync                  # клиент на ноутбуке (ask.py, блокнот gateway_client.ipynb)
```

`part_0_base/` — итог занятия 4 в трёх файлах (база, Job загрузки, агенты). Отличие одно: агент
берёт **всё** про LLM из окружения:

| Откуда | Переменная | Сейчас (часть 0) | Часть 2 | Часть 3 |
|---|---|---|---|---|
| ConfigMap `agents-config` | `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | `http://litellm:4000` | `http://litellm:4000` |
| ConfigMap `agents-config` | `MODEL` | `openai/gpt-6-luna` | `bank-llm` | `local-llm` |
| Secret `llm-key-<агент>` | `LLM_API_KEY` | ключ OpenRouter | виртуальный ключ LiteLLM | тот же |

```python
# images/agents/common.py — код больше не меняется, меняется только окружение
ChatOpenAI(model=os.getenv("MODEL"), base_url=os.getenv("LLM_BASE_URL"), api_key=os.environ["LLM_API_KEY"])
```

### Шаг 0.4. Проверка — решение по кредиту через Ingress

```bash
uv run python images/agents/ask.py http://risk.agents.test "Можно ли выдать счёту 2378 кредит 150 000 CZK на 24 месяца?"
```

> У `risk-manager` после `make base` бывает 2–3 перезапуска: он стартует раньше базы и падает на
> подключении к Postgres (память разговоров). Kubernetes перезапускает его, пока база не поднимется.

---

## Часть 1. LiteLLM — шлюз в кластере

### Шаг 1.1. База для шлюза

У шлюза своя база (виртуальные ключи, расходы, модели из UI) — в том же Postgres, что и данные банка:

```bash
kubectl exec deploy/bank-db -- psql -U postgres -c "CREATE DATABASE litellm"
```

### Шаг 1.2. LiteLLM

```bash
kubectl apply -f part_1_litellm/          # Secret + ConfigMap (config.yaml) + Redis + LiteLLM ×2 + Ingress
kubectl rollout status deploy/litellm     # первый старт ~1 мин: LiteLLM создаёт таблицы в базе
kubectl get pods -l 'app in (litellm,litellm-redis)' -o wide
```

| Файл | Что внутри |
|---|---|
| `secret.yaml` | `LITELLM_MASTER_KEY` (ключ администратора) и `LITELLM_SALT_KEY` (шифрует ключи провайдеров в базе — задаётся один раз) |
| `config.yaml` | `model_list`: `bank-llm` и `cheap-llm` → OpenRouter; повторы; Redis; `store_model_in_db` |
| `redis.yaml` | общее состояние реплик шлюза: счётчики лимитов, «охлаждение» моделей |
| `litellm.yaml` | Deployment (`ghcr.io/berriai/litellm:v1.106.3`) ×2, пробы `/health/*`, Service `litellm:4000`, Ingress `llm.agents.test` |

Почему так:

- **Две реплики** — шлюз теперь на пути каждого вызова LLM, одна реплика = единая точка отказа.
  Это возможно, потому что LiteLLM сам ничего не хранит: долговременное — в Postgres, оперативное — в Redis.
- **Без Redis** каждая реплика считает лимиты сама: `rpm_limit: 100` при двух репликах превращается
  в 200 (админка LiteLLM прямо предупреждает об этом баннером).
- **Ключ OpenRouter** лежит только в Secret `openrouter`, и видит его только шлюз.
- **Тег образа конкретный** (`v1.106.3` — последний на момент занятия). `main-latest` в проде — это
  «обновиться без предупреждения».

> **Mac M4 и новее:** без переменной `OPENSSL_armcap` в `litellm.yaml` под падает сразу (exit code 132,
> `Illegal instruction`, в логах пусто): OpenSSL 4 внутри образа неправильно определяет возможности
> процессора в виртуалке QEMU. Переменная задаёт их явно. На x86-серверах она ни на что не влияет.

### Шаг 1.3. Проверка: шлюз выглядит как OpenAI

```bash
curl -s http://llm.agents.test/v1/models -H "Authorization: Bearer sk-master-1234"
curl -s http://llm.agents.test/v1/chat/completions -H "Authorization: Bearer sk-master-1234" \
  -H "Content-Type: application/json" \
  -d '{"model": "bank-llm", "messages": [{"role": "user", "content": "Скажи привет одним словом"}]}'
```

### Шаг 1.4. Админка

http://llm.agents.test/ui — логин `admin`, пароль = master key (`sk-master-1234`).
Модели, виртуальные ключи, расходы, логи запросов, Playground.

---

## Часть 2. Агенты ходят в LLM через шлюз

### Шаг 2.1. Виртуальные ключи агентов

У каждого агента — **свой ключ** LiteLLM: доступ только к нужным моделям и бюджет $5.

```bash
make agent-keys          # POST /key/generate для analyst и risk → Secret-ы llm-key-analyst, llm-key-risk
```

### Шаг 2.2. Переключаем агентов на шлюз

```bash
kubectl apply -f part_2_agents_via_gateway/agents-config.yaml    # LLM_BASE_URL → litellm, MODEL → bank-llm
kubectl rollout restart deploy/analyst deploy/risk-manager       # env читается только при старте пода
kubectl rollout status deploy/analyst && kubectl rollout status deploy/risk-manager
```

Deployment-ы агентов **не менялись** — только ConfigMap и Secret.

### Шаг 2.3. Расходы по агентам

Задаём вопрос (`ask.py`, как в шаге 0.4, или блокнот `gateway_client.ipynb`, разделы 3–4) и смотрим
в админке **Virtual Keys** / **Usage**: расходы `analyst` и `risk` отдельно. Аналитик тратит в разы
больше — он делает SQL-запросы, риск-менеджер только задаёт вопросы и считает платёж.

> Расходы пишутся в базу пачками раз в несколько секунд, поэтому в админке они появляются с задержкой.

### Шаг 2.4. Свой ключ с ограничениями (блокнот, раздел 2)

Делаем себе ключ `student` и упираемся в его ограничения:

| Ограничение | Что происходит |
|---|---|
| `models: ["cheap-llm"]` | запрос к `bank-llm` → `403` |
| `rpm_limit: 3` | 4-й запрос за минуту → `429` (счётчик общий для обеих реплик — в Redis) |
| `max_budget: 0.05` | после $0.05 ключ перестаёт работать |

---

## Часть 3. Своя модель: vLLM на ноутбуке → шлюз → агенты

Поднимаем **свою** модель — Qwen3.5 9B на GPU ноутбука (vLLM + плагин vllm-metal для Apple
Silicon), даём ей имя внутри кластера и подключаем к шлюзу как модель `local-llm`. Агенты
переключаются на неё одной строкой конфига, а если модель недоступна — шлюз сам отправляет запрос
в OpenRouter.

```
 агенты ──▶ LiteLLM ──▶ local-llm:8080 (Service без selector) ──▶ 192.168.105.1:8080 — vLLM на Mac
                  └──(fallback, если local-llm не ответила)──▶ bank-llm (OpenRouter)
```

> **Альтернатива vLLM на Mac — oMLX:** та же модель, тот же порт и ключ, кластер не меняется —
> пошагово в [part_3_own_model/OMLX.md](part_3_own_model/OMLX.md).
>
> Своя модель на **ВМ с GPU** в облаке подключается так же: vLLM там запускают в Docker
> (`vllm/vllm-openai`), а в EndpointSlice (шаг 3.4) пишут IP ВМ вместо IP ноутбука.

### Шаг 3.1. Окружение vllm-metal (один раз)

```bash
curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh | bash   # → ~/.venv-vllm-metal
```

В `.env` впишите ключ к своей модели — любую строку (без ключа vLLM отвечает всем в локальной сети):

```bash
echo "VLLM_API_KEY=sk-vllm-$(openssl rand -hex 12)" >> .env
```

### Шаг 3.2. Запуск модели — в отдельном терминале

```bash
export HF_HUB_CACHE=/Volumes/aweeu_t7/hf_cache/hub     # где лежат/куда скачать веса (не задать — ~/.cache/huggingface/hub)
./part_3_own_model/run_vllm_mac.sh                      # ~1 мин: загрузка весов (~6 ГБ) → «Application startup complete»
```

Модель — [`lmstudio-community/Qwen3.5-9B-MLX-4bit`](https://huggingface.co/lmstudio-community/Qwen3.5-9B-MLX-4bit):
Qwen3.5 9B в 4-битной квантизации под MLX. Флаги запуска (каждый прокомментирован в
[run_vllm_mac.sh](part_3_own_model/run_vllm_mac.sh)):

| Флаг | Зачем |
|---|---|
| `--served-model-name qwen3.5-9b` | короткое имя модели в API — его укажем в LiteLLM |
| `--host 0.0.0.0 --port 8080` | слушать все интерфейсы: к модели будут ходить из кластера minikube |
| `--api-key …` | без ключа — `401` |
| `--max-model-len 32768` | максимум токенов на запрос (промпт + ответ); меньше — меньше памяти под KV-кэш |
| `--gpu-memory-utilization 0.3` | доля памяти GPU под vLLM (веса + KV-кэш) ≈ 15 ГБ. По умолчанию 0.92 — vLLM займёт почти всю память Mac, вместе с minikube начнётся своп, и генерация замедлится в разы |
| `--max-num-seqs 8` | сколько запросов одновременно (у нас 2 аналитика + 2 риск-менеджера) |
| `--max-num-batched-tokens 8192` | сколько токенов в одном шаге батча — ограничивает пики памяти |
| `--enable-chunked-prefill` | длинный промпт считается кусками и не блокирует генерацию у других запросов |
| `--enable-prefix-caching` | общее начало промптов (системный промпт агента, история) не пересчитывается |
| `--language-model-only` | Qwen3.5 мультимодальная — vision-часть нам не нужна, не грузим |
| `--reasoning-parser qwen3` | «мысли» модели (`<think>…</think>`) — отдельно, в поле `reasoning_content` |
| `--default-chat-template-kwargs '{"enable_thinking": false}'` | по умолчанию **не думать**: агентам важна скорость (включить на запрос: `"chat_template_kwargs": {"enable_thinking": true}`) |
| `--enable-auto-tool-choice` | модель сама решает, вызывать ли инструмент — **без этого агенты не работают** |
| `--tool-call-parser qwen3_coder` | как достать вызов инструмента из текста модели (формат Qwen3.5) |

### Шаг 3.3. Проверка модели напрямую (во втором терминале)

```bash
set -a; . ./.env; set +a                                 # VLLM_API_KEY в переменные окружения

curl -s localhost:8080/v1/models                         # без ключа → 401
curl -s localhost:8080/v1/models -H "Authorization: Bearer $VLLM_API_KEY"     # модель qwen3.5-9b

# генерация
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5-9b", "messages": [{"role": "user", "content": "Столица Чехии? Одним словом."}]}'

# вызов инструмента: в ответе должен быть tool_calls с loan_payment(amount=150000, months=24)
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $VLLM_API_KEY" \
  -H "Content-Type: application/json" -d '{
    "model": "qwen3.5-9b",
    "messages": [{"role": "user", "content": "Посчитай платёж по кредиту 150000 на 24 месяца"}],
    "tools": [{"type": "function", "function": {"name": "loan_payment", "description": "Monthly annuity payment",
      "parameters": {"type": "object", "properties": {"amount": {"type": "number"}, "months": {"type": "integer"}},
                     "required": ["amount", "months"]}}}]
  }'

curl -s localhost:8080/metrics | grep ^vllm: | head      # метрики vLLM (без ключа) — пригодятся в части 6
```

Нагрузку на модель удобно смотреть в [mlxtop](https://github.com/maximpri/mlxtop) — он сам ищет vLLM
на порту 8000, наш порт указываем явно (метрики он читает из `/metrics`, ключ не нужен):

```bash
MLXTOP_PROVIDER=vllm MLXTOP_PROVIDER_PORT=8080 mlxtop     # GEN / PREFILL tok/s, очередь, KV-кэш, память и своп Mac
```

### Шаг 3.4. Имя модели внутри кластера: Service без selector

```bash
kubectl apply -f part_3_own_model/local-llm-service.yaml
kubectl run t --rm -it --restart=Never --image=curlimages/curl:8.11.1 -- \
  -s -H "Authorization: Bearer $VLLM_API_KEY" http://local-llm:8080/v1/models   # модель видна из кластера
```

[local-llm-service.yaml](part_3_own_model/local-llm-service.yaml) — **Service без selector**: он
не ищет поды, а шлёт трафик на адрес из EndpointSlice, который мы задали руками. `192.168.105.1` —
это ноутбук, каким его видят ноды minikube (сеть `socket_vmnet`). Внутри кластера у модели появляется
обычное имя `local-llm:8080`; переедет модель на ВМ с GPU — правим одну строку с IP.

### Шаг 3.5. Модель в шлюзе: через UI или API

**UI:** http://llm.agents.test/ui → **Models + Endpoints** → **Add Model**:

| Поле | Значение |
|---|---|
| Provider | **Hosted vLLM** |
| LiteLLM Model Name | `qwen3.5-9b` (имя из `--served-model-name`) |
| Public Model Name | `local-llm` — так её будут звать агенты |
| API Base | `http://local-llm:8080/v1` |
| API Key | значение `VLLM_API_KEY` |

**Test Connect** проверит связь, **Add Model** сохранит. **API** — то же одной командой:

```bash
curl -s http://llm.agents.test/model/new -H "Authorization: Bearer sk-master-1234" \
  -H "Content-Type: application/json" -d "{
    \"model_name\": \"local-llm\",
    \"litellm_params\": {\"model\": \"hosted_vllm/qwen3.5-9b\", \"api_base\": \"http://local-llm:8080/v1\", \"api_key\": \"$VLLM_API_KEY\"}
  }"

# генерация через шлюз
curl -s http://llm.agents.test/v1/chat/completions -H "Authorization: Bearer sk-master-1234" \
  -H "Content-Type: application/json" \
  -d '{"model": "local-llm", "messages": [{"role": "user", "content": "Привет! Кто ты? Одним предложением."}]}'
```

Модель хранится **в базе** LiteLLM (`store_model_in_db: true`), а не в `config.yaml`: переживает
перезапуск и видна обеим репликам (они перечитывают список моделей раз в ~30 с). Ключ vLLM в базе
зашифрован salt key-ем — поэтому salt key нельзя менять после первого запуска.

### Шаг 3.6. Fallback: если своя модель недоступна — OpenRouter

```bash
kubectl apply -f part_3_own_model/litellm-config.yaml    # + router_settings.fallbacks: local-llm → bank-llm
kubectl rollout restart deploy/litellm && kubectl rollout status deploy/litellm
```

### Шаг 3.7. Переключаем агентов на свою модель

Виртуальные ключи агентов пускают только к `bank-llm` и `cheap-llm` — добавляем `local-llm`:

```bash
for agent in analyst risk; do
  KEY=$(kubectl get secret llm-key-$agent -o jsonpath='{.data.LLM_API_KEY}' | base64 -d)
  curl -s http://llm.agents.test/key/update -H "Authorization: Bearer sk-master-1234" \
    -H "Content-Type: application/json" -d "{\"key\": \"$KEY\", \"models\": [\"bank-llm\", \"cheap-llm\", \"local-llm\"]}"
done

kubectl apply -f part_3_own_model/agents-config.yaml    # MODEL: local-llm
kubectl rollout restart deploy/analyst deploy/risk-manager
kubectl rollout status deploy/analyst && kubectl rollout status deploy/risk-manager
```

Задаём вопрос — теперь думает **наша** модель (смотрите, как растёт `vllm:generation_tokens_total`):

```bash
uv run python images/agents/ask.py http://risk.agents.test "Можно ли выдать счёту 2378 кредит 150 000 CZK на 24 месяца?"
curl -s localhost:8080/metrics | grep -E "^vllm:(generation_tokens_total|request_success_total)"
```

Решение теперь занимает 1–2 мин, а при нескольких параллельных — до 4 мин (на OpenRouter — ~20 с):
одна локальная модель обслуживает двух аналитиков и двух риск-менеджеров, а на решение уходит
несколько тысяч токенов.

**Качество заметно ниже облачной модели** — это видно сразу, и это нормальная часть разговора о своей
модели. Что мы наблюдали на Qwen3.5 9B (без рассуждений): итоговое решение по 2378 и 1583 обычно
верное, но в цифрах бывают ошибки (вместо среднего баланса за год — последний остаток), а иногда
ответ противоречит сам себе («ОТКАЗАТЬ» в начале и «ИТОГ: ОДОБРИТЬ» в конце) или модель вместо решения
переспрашивает номер счёта. Что с этим делают: модель покрупнее (та же схема, другое имя в
`run_vllm_mac.sh`), режим рассуждений (`enable_thinking: true` — точнее, но в разы медленнее), более
жёсткий формат ответа в промпте, а для критичных решений — облачная модель через тот же шлюз.

### Шаг 3.8. Проверяем fallback

Остановите vLLM (**Ctrl-C** в его терминале) и задайте вопрос снова: ответ всё равно придёт — шлюз
повторил запрос на `bank-llm`. Видно по заголовку ответа шлюза:

```bash
curl -s -D - -o /dev/null http://llm.agents.test/v1/chat/completions -H "Authorization: Bearer sk-master-1234" \
  -H "Content-Type: application/json" -d '{"model": "local-llm", "messages": [{"role": "user", "content": "Привет"}]}' \
  | grep -i x-litellm-attempted-fallbacks                # x-litellm-attempted-fallbacks: 1
```

После 2 ошибок подряд шлюз 30 с вообще не шлёт запросы на `local-llm` (cooldown) — не тратит время
на мёртвую модель. Запустите vLLM снова (шаг 3.2) — через ≤ 30 с агенты вернутся на свою модель.

Другие варианты на той же механике: две записи с **одинаковым** `model_name` (своя модель и
OpenRouter) — шлюз балансирует между ними; `cheap-llm` → своя модель для простых задач, `bank-llm` —
облако для сложных.

---

## Часть 4. Langfuse в кластере (Helm)

Langfuse ставится официальным Helm-чартом `langfuse/langfuse` (2.1.6, Langfuse v4). Чарту нужны две
вещи в кластере (один раз на кластер): **cert-manager** и **оператор ClickHouse**.

### Шаг 4.1. cert-manager

Выписывает сертификаты — оператору ClickHouse они нужны для его вебхуков:

```bash
helm install cert-manager oci://quay.io/jetstack/charts/cert-manager --version v1.20.2 \
  -n cert-manager --create-namespace --set crds.enabled=true --wait
```

### Шаг 4.2. Оператор ClickHouse

Превращает ресурсы `ClickHouseCluster` / `KeeperCluster` в поды:

```bash
helm install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm --version 0.0.8 \
  -n clickhouse-operator --create-namespace --wait
kubectl get crd | grep clickhouse.com                     # новые типы ресурсов в кластере
```

### Шаг 4.3. Langfuse

```bash
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm repo update langfuse                                 # только этот репозиторий: один недоступный из других роняет общий update
helm install langfuse langfuse/langfuse --version 2.1.6 -n langfuse --create-namespace \
  -f part_4_langfuse/values.yaml                          # ~3–5 мин: качаются образы
kubectl get pods -n langfuse -w                           # web перезапускается пару раз, пока ждёт базы — это нормально; Ctrl-C
kubectl get clickhousecluster,keepercluster -n langfuse   # ресурсы, которые обслуживает оператор
```

В [part_4_langfuse/values.yaml](part_4_langfuse/values.yaml):

- **Ingress** `langfuse.agents.test` и `nextauth.url` — адрес UI;
- **headless-инициализация** (`LANGFUSE_INIT_*`): при первом старте Langfuse сам создаёт организацию,
  проект `bank-agents` с ключами `pk-lf-bank-agents` / `sk-lf-bank-agents` и пользователя — не нужно
  регистрироваться в UI и копировать ключи;
- все хранилища — в одной копии, тома по 1–5 ГБ (по умолчанию чарт рассчитан на прод: 20–100 ГБ,
  3 Keeper-а);
- пароли баз и ключи шифрования чарт генерирует сам (Secret-ы `langfuse-*` в namespace `langfuse`).

**Тома: StorageClass `local-path`, а не `standard`.** Postgres и Valkey чарта работают не от root, а
в папку стандартного тома minikube (`standard`) может писать только root — поды падают с
`Permission denied`. Поэтому `make setup` включает аддон `storage-provisioner-rancher` (класс
`local-path`): его тома доступны на запись и, в отличие от `standard`, **привязаны к своей ноде** —
под сам вернётся к данным после перезапуска (в занятии 4 мы делали это для `bank-db` руками через
`nodeSelector`). По умолчанию `local-path` кладёт данные в `/opt` — а это RAM-диск виртуалки
minikube; скрипт переносит их на настоящий диск ноды (`/data`).

### Шаг 4.4. Проверка

```bash
curl -s http://langfuse.agents.test/api/public/health                                      # {"status":"OK","version":"4.63.0"}
curl -s -u pk-lf-bank-agents:sk-lf-bank-agents http://langfuse.agents.test/api/public/projects   # проект bank-agents
```

**UI:** http://langfuse.agents.test — `admin@agents.test` / `admin12345`.

---

## Часть 5. Шлюз → Langfuse: трейс каждого вызова LLM

### Шаг 5.1. Включаем callback в LiteLLM

```bash
kubectl apply -f part_5_litellm_to_langfuse/            # Secret litellm-langfuse + config.yaml с callback
kubectl rollout restart deploy/litellm && kubectl rollout status deploy/litellm
kubectl logs -l app=litellm --prefix | grep -i "success callbacks"   # у каждого нового пода: ['langfuse']
```

```yaml
litellm_settings:
  success_callback: ["langfuse"]     # после каждого вызова — трейс в Langfuse
  failure_callback: ["langfuse"]     # и после неудачного тоже (видно, как сработал fallback)
```

Ключи и адрес Langfuse LiteLLM берёт из переменных `LANGFUSE_*` (Secret `litellm-langfuse`;
Deployment ссылается на него с `optional: true` с самого начала, поэтому менять его не пришлось).
Langfuse — в другом namespace, поэтому адрес полный: `langfuse-web.langfuse.svc.cluster.local:3000`.

### Шаг 5.2. Смотрим трейсы

```bash
uv run python images/agents/ask.py http://risk.agents.test "Можно ли выдать счёту 1 кредит 100 000 CZK на 24 месяца?"
```

**Langfuse → Tracing**: каждый вызов LLM — отдельный трейс `litellm:analyst` / `litellm:risk` (по
алиасу виртуального ключа) с промптом, ответом, токенами и задержкой. У вызовов `local-llm` модель —
`hosted_vllm/qwen3.5-9b`; если модель была остановлена, видно неудачный вызов и следом — `openrouter/…`
(fallback). Блокнот `gateway_client.ipynb` (раздел 5) достаёт последние вызовы через API Langfuse.

Это **вид со стороны шлюза**: видны все вызовы всех клиентов, но без структуры агента. Граф агента
целиком (какой инструмент, какой шаг) — это трейсинг из кода агента через SDK Langfuse, как в
занятии 3; оба подхода дополняют друг друга.

---

## Часть 6. Мониторинг: Prometheus + Grafana + метрики vLLM

vLLM сам отдаёт метрики в формате Prometheus на `/metrics` (без API-ключа): очередь запросов,
заполнение KV-кэша, токены, время до первого токена. Ставим стек мониторинга из занятия 4 и
показываем ему нашу модель.

### Шаг 6.1. kube-prometheus-stack + дашборд vLLM

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community
helm install prometheus prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f part_6_monitoring/values.yaml --version 65.1.1 --wait                  # ~2–3 мин
```

[part_6_monitoring/values.yaml](part_6_monitoring/values.yaml) — как в занятии 4 (Grafana и Prometheus
по Ingress, без Alertmanager) плюс **дашборд с grafana.com по номеру**: Grafana сама скачает при
старте дашборд **24756 «vLLM Monitoring - V2»** и подставит наш Prometheus:

```yaml
grafana:
  dashboards:
    grafana-com:
      vllm:
        gnetId: 24756              # номер дашборда на grafana.com
        datasource:                # в JSON дашборда источник данных — заглушка ${DS_PROMETHEUS},
        - name: DS_PROMETHEUS      #   меняем её на uid нашего Prometheus
          value: prometheus
prometheus:
  prometheusSpec:
    serviceDiscoveryRole: EndpointSlice   # искать цели через EndpointSlice — см. шаг 6.2
```

### Шаг 6.2. Prometheus собирает метрики модели

```bash
kubectl apply -f part_6_monitoring/local-llm-servicemonitor.yaml
```

**ServiceMonitor** — «что собирать»: все Service с меткой `app: local-llm`, порт `http`, путь
`/metrics`, раз в 15 с. Это наш Service без selector из шага 3.4 — Prometheus из кластера ходит за
метриками прямо к vLLM на ноутбук.

> По умолчанию Prometheus ищет адреса сервисов в объектах `Endpoints`, а у Service без selector
> есть только `EndpointSlice` (шаг 3.4). Без `serviceDiscoveryRole: EndpointSlice` в values
> ServiceMonitor находится, но его цель молча отбрасывается — графики пустые.

```bash
# через ~30 с: up = 1 — Prometheus достучался до модели
curl -s 'http://prometheus.agents.test/api/v1/query' --data-urlencode 'query=up{service="local-llm"}'
curl -s 'http://prometheus.agents.test/api/v1/query' --data-urlencode 'query=vllm:generation_tokens_total'
```

### Шаг 6.3. Дашборд

http://grafana.agents.test (admin / admin) → **Dashboards → LLM → vLLM Monitoring - V2**, сверху
`model_name` = `qwen3.5-9b`. Дайте нагрузку — несколько решений параллельно:

```bash
for account in 2378 1583 1 2378; do
  uv run python images/agents/ask.py http://risk.agents.test "Можно ли выдать счёту $account кредит 150 000 CZK на 24 месяца?" &
done; wait
```

Что смотреть на дашборде:

| Метрика | Что значит |
|---|---|
| Running / Waiting requests | запросов в работе и в очереди: очередь растёт — модель не успевает (`--max-num-seqs`) |
| KV cache usage | заполнение KV-кэша: под 100 % — запросы начнут вытесняться (preemptions) |
| Time to first token (TTFT) | сколько ждать первого токена — растёт с длиной промпта и очередью |
| Time per output token (TPOT) / Token Throughput | скорость генерации |
| Prefix cache hit rate | сколько промпта взято из кэша — у агентов высокий: системный промпт и история повторяются |

Остановите vLLM (Ctrl-C) — через ~30 с цель в Prometheus станет `down`, графики оборвутся,
а агенты продолжат работать через fallback (шаг 3.8).

---

## Грабли, на которые мы наступили (всё проверено на этом кластере)

| Симптом | Причина | Решение |
|---|---|---|
| `make setup` падает на `docker pull` образов ingress | сеть до registry.k8s.io рвётся | 3 попытки скачивания в `01_cluster.sh` |
| LiteLLM: `CrashLoopBackOff`, exit code 132, логи пустые | Mac M4+: OpenSSL 4 в образе падает на определении CPU в VM | `OPENSSL_armcap: "0x3d"` в `litellm.yaml` |
| `503` от `llm.agents.test` сразу после запуска LiteLLM | Ingress несколько секунд не знает про новые поды | подождать (скрипт `make agent-keys` ждёт сам) |
| Langfuse Postgres / Valkey: `Permission denied` | том `standard` пишет только root, а эти контейнеры не root | StorageClass `local-path` |
| трейсы не доходят, LiteLLM: `Connection timed out` к Langfuse | S3 (SeaweedFS) без места: тома `local-path` легли на RAM-диск `/opt`, а SeaweedFS нужно 7 × 1 ГБ сразу | путь `local-path` → `/data` (в `make setup`) |
| лимит `rpm_limit` пропускает больше запросов | у каждой реплики свой счётчик | Redis как кэш LiteLLM (`cache_params.type: redis`), ответы при этом не кэшируются (`mode: default_off`) |
| своя модель генерирует медленно, Mac уходит в своп | vLLM по умолчанию забирает 92 % памяти GPU, рядом minikube | `--gpu-memory-utilization 0.3` |
| mlxtop не видит vLLM | mlxtop ищет vLLM на порту 8000 | `MLXTOP_PROVIDER=vllm MLXTOP_PROVIDER_PORT=8080 mlxtop` |
| в Prometheus нет цели `local-llm` | Prometheus ищет цели через `Endpoints`, а у Service без selector только `EndpointSlice` | `serviceDiscoveryRole: EndpointSlice` в `part_6_monitoring/values.yaml` |
| дашборд vLLM пустой: «datasource ${DS_PROMETHEUS} not found» | в JSON с grafana.com источник — заглушка | `datasource: [{name: DS_PROMETHEUS, value: prometheus}]` |
| `kubectl logs deploy/litellm` не показывает новых строк | сразу после `rollout restart` команда может попасть в старый, ещё завершающийся под | `kubectl logs -l app=litellm --prefix` |
| агент на своей модели не вызывает инструменты | vLLM без tool calling | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |
| своя модель отвечает медленно | Qwen3.5 по умолчанию «думает» перед ответом | `--default-chat-template-kwargs '{"enable_thinking": false}'` |
| `403` от шлюза после переключения на `local-llm` | виртуальный ключ агента не пускает к новой модели | `/key/update` с новым списком `models` |
| новые ключи/конфиг не действуют | env и `config.yaml` читаются только при старте | `kubectl rollout restart` |

---

## Уборка

```bash
# Ctrl-C в терминале vLLM — остановить свою модель
helm uninstall prometheus -n monitoring; kubectl delete namespace monitoring
helm uninstall langfuse -n langfuse; kubectl delete namespace langfuse
helm uninstall clickhouse-operator -n clickhouse-operator; helm uninstall cert-manager -n cert-manager
make clean                                        # удалить кластер целиком (minikube delete)
```

## Файлы

```
Makefile, scripts/                 кластер (+ local-path), /etc/hosts, образ, Secret-ы, ключи агентов
images/agents/                     агенты занятия 4; common.py: LLM_BASE_URL / LLM_API_KEY / MODEL из окружения
part_0_base/                       весь стек занятия 4: база, загрузка данных, MCP, аналитик, риск-менеджер, Ingress
part_1_litellm/                    LiteLLM: Secret, config.yaml, Redis, Deployment ×2 + Service + Ingress
part_2_agents_via_gateway/         ConfigMap агентов → шлюз
part_3_own_model/                  run_vllm_mac.sh (vLLM на Mac), OMLX.md (альтернатива — oMLX), Service + EndpointSlice local-llm,
                                   fallback, агенты → local-llm
part_4_langfuse/values.yaml        Helm-значения Langfuse
part_5_litellm_to_langfuse/        Secret с ключами Langfuse + config.yaml с callback
part_6_monitoring/                 Helm-значения kube-prometheus-stack (+ дашборд vLLM 24756), ServiceMonitor для local-llm
gateway_client.ipynb               шлюз как OpenAI API, свой ключ с лимитами, расходы, трейсы
```
