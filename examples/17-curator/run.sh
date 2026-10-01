#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
export HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' '请先在仓库根目录执行 bash scripts/setup_deps.sh，准备 Hermes 依赖。' >&2
  exit 1
fi
export HERMES_PYTHON="$PYTHON"
export PYTHONPATH="$HERMES_SRC${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON" -B "$HERE/curation_workshop.py" "$@"
