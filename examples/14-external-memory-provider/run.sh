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
MODE="${1:-file}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "$MODE" == "native" ]]; then
  exec "$PYTHON" -B "$HERE/run_native_openviking.py" "$@"
fi
case "$MODE" in
  file) exec "$PYTHON" -B "$HERE/run_provider.py" "$@" ;;
  sqlite) exec "$PYTHON" -B "$HERE/run_shared_sqlite.py" "$@" ;;
  check) exec "$PYTHON" -B "$HERE/test_offline.py" "$@" ;;
  *) printf '%s\n' '用法：run.sh [file|sqlite] [--with-model]；进阶：run.sh native [--live ...]' >&2; exit 2 ;;
esac
