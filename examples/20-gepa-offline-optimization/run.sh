#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONDONTWRITEBYTECODE=1
PYTHON="${GEPA_PYTHON:-python3}"
if [[ "${1:---offline}" == "--offline" ]]; then
  exec "$PYTHON" -B -m unittest -v test_offline test_enterprise
fi
exec "$PYTHON" -B optimize.py "$@"
