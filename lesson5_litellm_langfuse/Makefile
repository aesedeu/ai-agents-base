# Занятие 5 — LLM-шлюз LiteLLM и Langfuse в Kubernetes
.PHONY: setup hosts images secret base agent-keys clean

setup:      ## кластер: 3 ноды minikube на QEMU + аддоны (ingress, metrics-server, local-path)
	@./scripts/01_cluster.sh

hosts:      ## *.agents.test → IP кластера в /etc/hosts (нужен sudo)
	@./scripts/02_update_hosts.sh

images:     ## собрать образ агентов на всех нодах
	@./scripts/03_images.sh

secret:     ## ключ OpenRouter из .env → Secret-ы openrouter, llm-key-analyst, llm-key-risk
	@./scripts/04_secret.sh

base:       ## часть 0: весь стек занятия 4 (база, MCP, аналитик, риск-менеджер, Ingress)
	kubectl apply -f part_0_base/
	kubectl wait --for=condition=complete job/bank-seed --timeout=600s
	kubectl rollout status deploy/bank-mcp deploy/analyst deploy/risk-manager --timeout=300s

agent-keys: ## часть 2: виртуальные ключи LiteLLM для агентов → Secret-ы llm-key-*
	@./scripts/05_agent_keys.sh

clean:      ## удалить кластер целиком
	minikube delete
