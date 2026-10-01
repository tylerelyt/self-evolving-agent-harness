#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON="${HERMES_PYTHON:-$HERE/../../.deps/hermes-agent/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' '未找到 Hermes 解释器，请先在仓库根目录执行 bash scripts/setup_deps.sh。' >&2
  exit 1
fi
export HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
export PYTHONPATH="$HERMES_SRC${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
export HERMES_PYTHON="$PYTHON"
exec "$PYTHON" -B "$HERE/native_workshop.py" "$@"
