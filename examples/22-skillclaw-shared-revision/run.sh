#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1
PYTHON="$ROOT/.deps/hermes-agent/.venv/bin/python"
if [[ "${1-}" == '--help' || "${1-}" == '-h' ]]; then
  exec "${PYTHON:-python3}" -B "$HERE/run22.py" --help
fi
bash "$ROOT/scripts/setup_skillclaw.sh" --check
if [[ "${1-}" == '--offline' ]]; then
  if [[ $# -ne 1 ]]; then echo '--offline 不接受其他参数。' >&2; exit 2; fi
  exec "$PYTHON" -B "$HERE/test_run22.py"
fi
exec "$PYTHON" -B "$HERE/run22.py" "$@"
