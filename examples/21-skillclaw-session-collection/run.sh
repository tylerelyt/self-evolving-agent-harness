#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
export PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1
bash "$ROOT/scripts/setup_skillclaw.sh" --check
if [[ "${1-}" == '--offline' ]]; then
  if [[ $# -ne 1 ]]; then echo '--offline 不接受其他参数。' >&2; exit 2; fi
  exec "$ROOT/.deps/hermes-agent/.venv/bin/python" -B "$HERE/test_run21.py"
fi
exec "$ROOT/.deps/hermes-agent/.venv/bin/python" -B "$HERE/run21.py" "$@"
