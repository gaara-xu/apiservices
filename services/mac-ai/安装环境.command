#!/bin/bash
# 在项目内创建Python环境并安装模型服务依赖，安装完成前不启动服务。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p runtime/tmp runtime/cache
export TMPDIR="$PWD/runtime/tmp" PIP_CACHE_DIR="$PWD/runtime/cache" PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.11}"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.lock
printf '环境安装完成，可运行启动.command\n'
