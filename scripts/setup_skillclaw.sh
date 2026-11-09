#!/usr/bin/env bash
# 第21—23讲使用固定 SkillClaw 源码；不改个人 Hermes / SkillClaw 配置。
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"
SOURCE="$ROOT/.deps/SkillClaw"
REVISION="$(tr -d '\r\n' < "$HERE/skillclaw-revision.txt")"
CHECK=false
case "${1-}" in
  '') ;;
  --check) CHECK=true ;;
  *) echo '用法：bash scripts/setup_skillclaw.sh [--check]' >&2; exit 2 ;;
esac
bash "$HERE/setup_deps.sh" --check
PROXY="${PROXY-}"
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
if [[ -n "$PROXY" ]]; then
  export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" http_proxy="$PROXY" https_proxy="$PROXY"
fi
if [[ ! -e "$SOURCE" ]]; then
  if $CHECK; then
    echo '请先运行 bash scripts/setup_skillclaw.sh。' >&2; exit 1
  fi
  TEMP="$(mktemp -d "$ROOT/.deps/skillclaw-fetch.XXXXXX")"
  trap 'rm -rf -- "$TEMP"' EXIT
  git init -q "$TEMP"
  git -C "$TEMP" remote add origin https://github.com/AMAP-ML/SkillClaw.git
  git -C "$TEMP" -c http.proxy="$PROXY" fetch --depth 1 origin "$REVISION"
  git -C "$TEMP" checkout --detach "$REVISION"
  mv "$TEMP" "$SOURCE"
  trap - EXIT
fi
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$REVISION" ]] ||
   [[ -n "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ]]; then
  echo 'SkillClaw 与固定版本不符或已有源码修改；未覆盖本机文件。' >&2; exit 1
fi
PYTHON="$ROOT/.deps/hermes-agent/.venv/bin/python"
if ! $CHECK; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON" -e "$SOURCE[evolve]"
  else
    if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
      "$PYTHON" -m ensurepip --upgrade
    fi
    "$PYTHON" -m pip --proxy "$PROXY" install -e "$SOURCE[evolve]"
  fi
fi
"$PYTHON" -IB -c 'from skillclaw.api_server import SkillClawAPIServer; from skillclaw.validation_worker import ValidationWorker; from evolve_server.engines.workflow import EvolveServer; print("SkillClaw 固定源码与代理、复盘、验证依赖检查通过。")'
