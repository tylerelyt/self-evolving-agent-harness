#!/usr/bin/env bash
# 第12讲单独保存现场、会话和记忆，复用常驻 Hermes 的服务实现。
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
export ONCALL_LESSON=12
if [[ "${1:-}" == "status" || "${1:-}" == "down" ]]; then
  exec "$HERE/../../workshop/oncall/lab" "$@"
fi
if [[ "${1:-}" == "check" ]]; then
  shift
  HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
  PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
  export HERMES_SRC
  exec "$PYTHON" -B "$HERE/run_memory_revision.py" --check-local "$@"
fi
exec "$HERE/../../workshop/oncall/run.sh" "$@"
