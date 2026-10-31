#!/usr/bin/env bash
# 使用宿主机网络和代理构建部署，通过独立端口预检后切换，失败恢复旧服务。
set -Eeuo pipefail
cd "$(dirname "$0")"
VERSION="${1:-$(cat VERSION)}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9]+)*$ ]] || { echo '版本号格式无效'; exit 2; }
IMAGE="apiserver:$VERSION"
BASE='python:3.10-slim-bullseye'
RESOURCE_DIR="${RESOURCE_DIR:-/gaara/APIservicesResource}"
ENV_FILE="${ENV_FILE:-$PWD/.env}"
CANDIDATE="apiserver-candidate-$$"
BACKUP="apiserver-previous-$$"
OLD_IMAGE=''
OLD_STOPPED=0
NEW_STARTED=0
SUCCESS=0
CANDIDATE_PORT="${CANDIDATE_PORT:-18110}"
[[ "$CANDIDATE_PORT" =~ ^[0-9]+$ ]] && (( CANDIDATE_PORT > 1024 && CANDIDATE_PORT < 65536 && CANDIDATE_PORT != 8110 )) || { echo '候选端口无效'; exit 2; }
PROXY_URL="${PROXY_URL:-http://127.0.0.1:1087}"
NO_PROXY_LIST="localhost,127.0.0.1,::1,192.168.3.110${NO_PROXY_EXTRA:+,$NO_PROXY_EXTRA}"
NETWORK_ARGS=(--network host -e "http_proxy=$PROXY_URL" -e "https_proxy=$PROXY_URL" -e "HTTP_PROXY=$PROXY_URL" -e "HTTPS_PROXY=$PROXY_URL" -e "no_proxy=$NO_PROXY_LIST" -e "NO_PROXY=$NO_PROXY_LIST")
mkdir -p "$RESOURCE_DIR/tmp" "$RESOURCE_DIR/deploy-logs"
exec 9>"$RESOURCE_DIR/deploy.lock"
flock -n 9 || { echo '其他部署正在执行'; exit 1; }
LOG="$RESOURCE_DIR/deploy-logs/$VERSION-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
[[ -f "$ENV_FILE" ]] || { echo '缺少 .env，请参考 .env.example'; exit 1; }
[[ -d wheelhouse ]] || { echo '缺少离线 wheelhouse 依赖目录'; exit 1; }
docker image inspect "$BASE" >/dev/null
action_label='org.apiserver.managed=true'
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "版本 $IMAGE 已存在，请使用新版本号"; exit 1
fi
if docker container inspect apiserver >/dev/null 2>&1; then
    [[ "$(docker inspect -f '{{index .Config.Labels "org.apiserver.managed"}}' apiserver)" == true ]] || { echo '同名容器不属于本服务'; exit 1; }
    OLD_IMAGE="$(docker inspect -f '{{.Config.Image}}' apiserver)"
    [[ "$OLD_IMAGE" == apiserver:* ]] || { echo '旧镜像名称不符合管理规则'; exit 1; }
fi
BEFORE_IMAGES="$(docker image ls -aq --no-trunc)"

# 仅清理本次新产生、属于本服务的悬空镜像，保留 Python 基础镜像及其他项目镜像。
cleanup_dangling() {
    local id
    while read -r id; do
        [[ -z "$id" ]] && continue
        if ! grep -Fxq "$id" <<< "$BEFORE_IMAGES"; then
            docker image rm "$id" || true
        fi
    done < <(docker image ls -q --no-trunc --filter dangling=true --filter "label=$action_label")
}

# 在异常退出时清理失败版本，并恢复已停止的旧容器。
finish() {
    local code=$?
    trap - EXIT
    docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
    if [[ "$SUCCESS" == 0 ]]; then
        if [[ "$NEW_STARTED" == 1 ]]; then docker rm -f apiserver || true; fi
        if [[ "$OLD_STOPPED" == 1 ]]; then
            if docker container inspect "$BACKUP" >/dev/null 2>&1; then docker rename "$BACKUP" apiserver || true; fi
            docker start apiserver || true
        fi
        docker image rm "$IMAGE" >/dev/null 2>&1 || true
        echo '部署失败：已清理本次失败版本，保留并尝试恢复旧服务；数据库不回退。'
    fi
    cleanup_dangling
    exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 直连候选端口并核对版本与数据库就绪，避免误判旧服务为新服务。
wait_ready() {
    local name="$1" port="${2:-8110}" attempt
    for attempt in {1..30}; do
        if docker exec "$name" python -c "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); v=json.load(o.open('http://127.0.0.1:$port/health/live',timeout=3)); assert v['version']=='$VERSION'; o.open('http://127.0.0.1:$port/health/ready',timeout=3)" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    docker logs "$name" || true
    return 1
}

docker build --pull=false --force-rm --build-arg "APP_VERSION=$VERSION" -t "$IMAGE" -f services/api/Dockerfile .
docker run --rm --env-file "$ENV_FILE" "${NETWORK_ARGS[@]}" -v "$RESOURCE_DIR:/resources" "$IMAGE" python -m app.migrate
docker run -d --name "$CANDIDATE" --label "$action_label" --env-file "$ENV_FILE" "${NETWORK_ARGS[@]}" --no-healthcheck -v "$RESOURCE_DIR:/resources" "$IMAGE" uvicorn app.main:app --host 127.0.0.1 --port "$CANDIDATE_PORT"
wait_ready "$CANDIDATE" "$CANDIDATE_PORT"
docker rm -f "$CANDIDATE"
if [[ -n "$OLD_IMAGE" ]]; then
    docker stop apiserver
    OLD_STOPPED=1
    docker rename apiserver "$BACKUP"
fi
NEW_STARTED=1
docker run -d --name apiserver --label "$action_label" --restart always --env-file "$ENV_FILE" "${NETWORK_ARGS[@]}" -v "$RESOURCE_DIR:/resources" "$IMAGE"
wait_ready apiserver
SUCCESS=1
if [[ -n "$OLD_IMAGE" ]]; then
    docker rm "$BACKUP"
    docker image rm "$OLD_IMAGE"
fi
# 仅删除当前版本明确命名的临时部署包，不扫描或删除其他业务文件。
rm -f "$RESOURCE_DIR/apiserver-$VERSION.tar.gz"
echo "部署成功：$IMAGE，host网络，端口8110，临时部署包已清理"
