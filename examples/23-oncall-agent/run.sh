#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
export PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1
bash "$ROOT/scripts/setup_skillclaw.sh" --check
if [[ "${1-}" == '--offline' ]]; then
  if [[ $# -ne 1 ]]; then echo '--offline only runs local checks; use a separate command for a new exercise run.' >&2; exit 2; fi
  exec "$ROOT/.deps/hermes-agent/.venv/bin/python" -B "$HERE/test_run23.py"
fi
"$ROOT/.deps/hermes-agent/.venv/bin/python" -B "$HERE/setup_openviking.py" --check
"$ROOT/.deps/hermes-agent/.venv/bin/python" -IB -c 'from importlib.metadata import version; assert version("gepa") == "0.0.27", "Install examples/23-oncall-agent/requirements.txt in the Hermes environment"'
exec "$ROOT/.deps/hermes-agent/.venv/bin/python" -B "$HERE/run23.py" "$@"
