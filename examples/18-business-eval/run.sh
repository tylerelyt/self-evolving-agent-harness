#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "${1:-}" == "--offline" ]]; then
  exec python3 -B "$HERE/alert_workshop.py" "$@"
fi
export HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
export HERMES_PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
export PYTHONPATH="$HERMES_SRC${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
unset BIGMODEL_API_KEY
unset http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
if [[ ! -x "$HERMES_PYTHON" ]]; then
  printf '%s\n' '请先在工程根目录执行 bash scripts/setup_deps.sh，准备 Hermes 依赖。' >&2
  exit 1
fi
exec "$HERMES_PYTHON" -B "$HERE/alert_workshop.py" "$@"
