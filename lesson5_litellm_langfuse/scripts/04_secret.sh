#!/bin/bash
# Ключ OpenRouter из .env → Secret-ы кластера (в git и в манифесты ключ не попадает):
#   openrouter       OPENROUTER_API_KEY — им пользуется LiteLLM (часть 1)
#   llm-key-analyst  LLM_API_KEY ┐ ключи агентов. На старте (часть 0) это тот же ключ OpenRouter,
#   llm-key-risk     LLM_API_KEY ┘ в части 2 заменим их виртуальными ключами LiteLLM
set -e
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "❌ нет .env: cp .env.example .env и впишите OPENROUTER_API_KEY"; exit 1; }
set -a; . ./.env; set +a
[ -n "$OPENROUTER_API_KEY" ] || { echo "❌ в .env пустой OPENROUTER_API_KEY"; exit 1; }

# create … --dry-run | apply — создать, а если уже есть, обновить
kubectl create secret generic openrouter --from-literal=OPENROUTER_API_KEY="$OPENROUTER_API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
for agent in analyst risk; do
    kubectl create secret generic "llm-key-$agent" --from-literal=LLM_API_KEY="$OPENROUTER_API_KEY" \
        --dry-run=client -o yaml | kubectl apply -f -
done
echo "✅ Secret-ы openrouter, llm-key-analyst, llm-key-risk готовы"
