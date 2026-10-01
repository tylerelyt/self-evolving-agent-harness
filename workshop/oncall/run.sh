#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="${HERMES_PYTHON:-$HERE/../../.deps/hermes-agent/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' '未找到 Hermes 解释器。请先运行工程 scripts/setup_deps.sh 安装固定依赖。' >&2
  exit 1
fi
exec "$PYTHON" -B "$HERE/demo.py" "$@"
