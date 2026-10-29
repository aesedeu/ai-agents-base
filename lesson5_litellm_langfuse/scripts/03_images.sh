#!/bin/bash
# Собирает образ агентов прямо в кластере, сразу на всех нодах (Docker на Mac не нужен).
set -e
cd "$(dirname "$0")/.."

echo "🔨 bank-agents:1 — MCP-сервер, аналитик, риск-менеджер, загрузчик данных"
minikube image build --all -t bank-agents:1 images/agents

echo "✅ Образы на нодах:"
minikube image ls | grep bank-agents
