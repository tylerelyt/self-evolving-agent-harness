#!/usr/bin/env bash
# 每次独立运行；只读取调用方传入的配置，不读取个人凭据文件。
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export HERMES_SRC="${HERMES_SRC:-$HERE/../../.deps/hermes-agent}"
PYTHON="${HERMES_PYTHON:-$HERMES_SRC/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo '未找到 Hermes 解释器，请先运行仓库 scripts/setup_deps.sh。' >&2
  exit 1
fi
exec "$PYTHON" -B "$HERE/run_background_review.py" "$@"
