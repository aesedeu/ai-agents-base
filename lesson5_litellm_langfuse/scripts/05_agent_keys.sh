#!/bin/bash
# Виртуальные ключи LiteLLM для агентов → Secret-ы llm-key-analyst и llm-key-risk.
# У каждого агента свой ключ: в LiteLLM видно, кто сколько запросов сделал и сколько потратил,
# ключу можно ограничить модели и бюджет, а отозвать его — не трогая ключ OpenRouter.
set -e
IP=$(minikube ip)
LLM="http://llm.agents.test"
CURL="curl -sf --resolve llm.agents.test:80:$IP"          # через Ingress, даже если /etc/hosts не настроен
MASTER=$(kubectl get secret litellm-secret -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# сразу после запуска LiteLLM Ingress ещё несколько секунд отвечает 503 — ждём, пока шлюз станет доступен
until $CURL -o /dev/null "$LLM/health/readiness"; do echo "⏳ жду LiteLLM через Ingress…"; sleep 2; done

for agent in analyst risk; do
    # повторный запуск: старый ключ агента удаляем (key_alias у ключей уникален)
    $CURL "$LLM/key/delete" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"key_aliases\": [\"$agent\"]}" >/dev/null 2>&1 || true

    KEY=$($CURL "$LLM/key/generate" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"key_alias\": \"$agent\", \"models\": [\"bank-llm\", \"cheap-llm\"], \"max_budget\": 5}" \
        | python3 -c "import json, sys; print(json.load(sys.stdin)['key'])")

    kubectl create secret generic "llm-key-$agent" --from-literal=LLM_API_KEY="$KEY" \
        --dry-run=client -o yaml | kubectl apply -f -
    echo "🔑 $agent: ключ ${KEY:0:7}… (модели bank-llm, cheap-llm; бюджет \$5)"
done
echo "✅ Агенты увидят новые ключи после перезапуска: kubectl rollout restart deploy/analyst deploy/risk-manager"
