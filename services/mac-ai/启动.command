#!/bin/bash
# 使用项目虚拟环境执行服务启动操作，所有运行文件保存在项目内。
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
if [ ! -x .venv/bin/python ]; then
    printf '请先运行安装环境.command\n'
    exit 1
fi
exec .venv/bin/python manage.py start
