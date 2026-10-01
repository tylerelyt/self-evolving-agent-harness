#!/usr/bin/env bash
# 第12讲主入口已改为真实常驻服务；旧机制脚本仍保留供历史核查。
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$HERE/../../workshop/oncall/run.sh" "$@"
