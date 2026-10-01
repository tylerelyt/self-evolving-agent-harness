#!/usr/bin/env bash
# 从官方 wheel 安装到第14讲独立环境；--check 不联网。
# PROXY 可显式指定安装代理；默认直连，不改变系统代理设置。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
PYTHON="${PYTHON:-python3.12}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo '请先准备 Python 3.11/3.12，并用 PYTHON 指定解释器；不会自动下载 Python。' >&2
  exit 1
fi
exec "$PYTHON" -B "$ROOT/examples/14-external-memory-provider/setup_native_env.py" "$@"
