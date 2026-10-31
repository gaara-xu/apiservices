#!/usr/bin/env bash
# 构建并健康检查新版本，成功清理旧版本，失败只清理本次构建并恢复旧服务。
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

# 在容器内部检测服务和数据库就绪，失败时输出容器日志。
wait_ready() {
    local name="$1" attempt
    for attempt in {1..30}; do
        if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8110/health/ready',timeout=3)" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    docker logs "$name" || true
    return 1
}

docker build --pull=false --force-rm --build-arg "APP_VERSION=$VERSION" -t "$IMAGE" -f services/api/Dockerfile .
docker run --rm --env-file "$ENV_FILE" -v "$RESOURCE_DIR:/resources" "$IMAGE" python -m app.migrate
docker run -d --name "$CANDIDATE" --label "$action_label" --env-file "$ENV_FILE" -v "$RESOURCE_DIR:/resources" "$IMAGE"
wait_ready "$CANDIDATE"
docker rm -f "$CANDIDATE"
if [[ -n "$OLD_IMAGE" ]]; then
    docker stop apiserver
    OLD_STOPPED=1
    docker rename apiserver "$BACKUP"
fi
NEW_STARTED=1
docker run -d --name apiserver --label "$action_label" --restart unless-stopped --env-file "$ENV_FILE" -p 8110:8110 -v "$RESOURCE_DIR:/resources" "$IMAGE"
wait_ready apiserver
SUCCESS=1
if [[ -n "$OLD_IMAGE" ]]; then
    docker rm "$BACKUP"
    docker image rm "$OLD_IMAGE"
fi
echo "部署成功：$IMAGE，端口8110"
