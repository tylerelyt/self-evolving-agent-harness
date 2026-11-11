#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
export PYTHON_DOTENV_DISABLED=1 PYTHONDONTWRITEBYTECODE=1
PYTHON="$ROOT/.deps/hermes-agent/.venv/bin/python"
if [[ "${1-}" == '--offline' ]]; then
  if [[ $# -ne 1 ]]; then echo '--offline only runs local checks; use a separate command for a new exercise run.' >&2; exit 2; fi
  bash "$ROOT/scripts/setup_skillclaw.sh" --check
  "$PYTHON" -B "$HERE/test_run23.py"
  exec "$PYTHON" -B "$HERE/test_steps.py"
fi
if [[ "${1-}" == '--reference' ]]; then
  shift
  if [[ "${1-}" == '--help' || "${1-}" == '-h' ]]; then
    if [[ ! -x "$PYTHON" ]]; then PYTHON="python3"; fi
    exec "$PYTHON" -B "$HERE/run23.py" "$@"
  fi
  bash "$ROOT/scripts/setup_skillclaw.sh" --check
  "$PYTHON" -B "$HERE/setup_openviking.py" --check
  "$PYTHON" -IB -c 'from importlib.metadata import version; assert version("gepa") == "0.0.27", "Install examples/23-oncall-agent/requirements.txt in the Hermes environment"'
  exec "$PYTHON" -B "$HERE/run23.py" "$@"
fi
if [[ ! -x "$PYTHON" ]]; then PYTHON="python3"; fi
exec "$PYTHON" -B "$HERE/steps.py" "$@"
