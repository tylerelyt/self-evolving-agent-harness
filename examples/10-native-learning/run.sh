#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
export HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo '请先在仓库根目录运行 bash scripts/setup_deps.sh。' >&2
  exit 1
fi
exec "$PYTHON" -B "$HERE/lab.py" "$@"
