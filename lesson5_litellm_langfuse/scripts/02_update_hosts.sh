#!/bin/bash
# Прописывает учебные домены *.agents.test на IP кластера в /etc/hosts (спросит пароль sudo).
set -e
IP=$(minikube ip 2>/dev/null) || { echo "❌ minikube не запущен: make setup"; exit 1; }
HOSTS="risk.agents.test llm.agents.test langfuse.agents.test grafana.agents.test prometheus.agents.test"

TMP=$(mktemp)
grep -v "agents.test" /etc/hosts > "$TMP"                # убираем старые записи
echo "$IP  $HOSTS" >> "$TMP"
sudo cp "$TMP" /etc/hosts && rm "$TMP"

echo "✅ /etc/hosts: $IP → $HOSTS"
