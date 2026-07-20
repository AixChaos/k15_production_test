#!/usr/bin/env bash
# Ubuntu 22.04 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "创建虚拟环境并安装依赖..."
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv
    uv pip install -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -U pip
    .venv/bin/pip install -r requirements.txt
  fi
fi

exec .venv/bin/python main.py "$@"
