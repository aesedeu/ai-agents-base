#!/bin/bash
# Учебный кластер: 3 ноды minikube на QEMU (macOS) + аддоны (как в занятии 4, но ноды крупнее).
# Нужен сетевой демон socket_vmnet (один раз): sudo brew services start socket_vmnet
set -e

if [ ! -S /opt/homebrew/var/run/socket_vmnet ]; then
    echo "❌ socket_vmnet не запущен. Запустите: sudo brew services start socket_vmnet"
    exit 1
fi

minikube delete >/dev/null 2>&1 || true

echo "🚀 Запускаем minikube: 3 ноды на QEMU…"
# 3 × (4 CPU, 8 ГБ RAM, 40 ГБ диска): кроме агентов в кластере будут LiteLLM и Langfuse
# (а у Langfuse свои Postgres, ClickHouse, Redis и S3) — в занятии 4 ноды были вдвое меньше
minikube start --driver qemu --network socket_vmnet --nodes 3 --cpus 4 --memory 8g --disk-size 40g

echo "📦 Заранее качаем образы ingress на все ноды"
# kubelet бросает скачивание, если оно минуту не продвигается; при медленном registry.k8s.io
# ingress-контроллер тогда висит в ImagePullBackOff. docker pull на ноде такого лимита не имеет.
for image in $(minikube addons images ingress 2>/dev/null | awk -F'|' 'NF > 3 && $3 ~ /@sha256/ {gsub(/ /, ""); print $4 "/" $3}' | sort -u); do
    for node in $(kubectl get nodes -o name | cut -d/ -f2); do
        echo "   $node ← $image"
        for attempt in 1 2 3; do                              # сеть до реестра иногда рвётся — 3 попытки
            minikube ssh -n "$node" -- docker pull -q "$image" >/dev/null && break
            [ $attempt = 3 ] && { echo "❌ не скачался $image"; exit 1; }
            echo "   ⚠️  не вышло, ещё раз…"; sleep 5
        done
    done
done

echo "🧩 Аддоны: ingress (вход по домену) и metrics-server (kubectl top, автоскейлинг)"
minikube addons enable ingress || { echo "⚠️  ingress не успел подняться, пробуем ещё раз…"; minikube addons enable ingress; }
minikube addons enable metrics-server

echo "💾 Аддон storage-provisioner-rancher: StorageClass local-path (для Langfuse, часть 4)"
# В отличие от standard: в том может писать не-root контейнер, и том привязан к своей ноде.
minikube addons enable storage-provisioner-rancher
# По умолчанию local-path кладёт тома в /opt — а это RAM-диск виртуалки minikube (данные пропадут
# при перезагрузке и съедят память). Переносим на настоящий диск ноды — /data
kubectl -n local-path-storage patch configmap local-path-config --type merge -p \
    '{"data":{"config.json":"{\"nodePathMap\":[{\"node\":\"DEFAULT_PATH_FOR_NON_LISTED_NODES\",\"paths\":[\"/data/local-path-provisioner\"]}]}"}}'
kubectl -n local-path-storage rollout restart deploy/local-path-provisioner     # перечитать конфиг
# Аддон делает local-path классом по умолчанию — возвращаем standard, как было в занятии 4
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
kubectl patch storageclass standard -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

kubectl get nodes -o wide
echo "✅ Кластер готов. IP: $(minikube ip)"
