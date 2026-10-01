#!/usr/bin/env bash
# 为第 11—12 讲获取 Hermes 源码，并准备共用的 Python 环境。
# 用法：bash scripts/setup_deps.sh
#       PROXY=http://127.0.0.1:7890 bash scripts/setup_deps.sh
#       PROXY= bash scripts/setup_deps.sh  # 直连
# 新建环境使用 Python 3.12；已有环境可复用上游支持的 Python 3.11—3.13。
# 已有源码目录必须与固定提交一致；不覆盖或自动更新本机源码。
# 本脚本不需要 API key，也不会读取或写入任何 API key 配置。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$HERE/.." && pwd)"
DEPS_DIR="$REPO_ROOT/.deps"
HERMES_COMMIT="$(tr -d '\r\n' < "$HERE/hermes-revision.txt")"
PROXY="${PROXY-}"
CHECK_ONLY=false
case "${1-}" in
  '') ;;
  --check) CHECK_ONLY=true ;;
  *) echo '用法：bash scripts/setup_deps.sh [--check]' >&2; exit 2 ;;
esac
if [[ ! "$HERMES_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo '错误：scripts/hermes-revision.txt 必须包含完整的40位提交编号。' >&2
  exit 1
fi

# 同时覆盖大小写变量，避免继承的 SOCKS 代理或 NO_PROXY 绕过本次设置。
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="localhost,127.0.0.1,::1" no_proxy="localhost,127.0.0.1,::1"
export PIP_PROXY="$PROXY"
if [[ -n "$PROXY" ]]; then
  export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
  export http_proxy="$PROXY" https_proxy="$PROXY"
  echo "联网步骤使用 PROXY 指定的代理；不设置 PROXY 时默认直连。"
else
  echo "联网步骤使用直连。"
fi

if ! command -v git >/dev/null 2>&1; then
  echo "错误：请先安装 Git。" >&2
  exit 1
fi

HERMES_SRC="$DEPS_DIR/hermes-agent"
if [[ ! -e "$HERMES_SRC" ]]; then
  if $CHECK_ONLY; then
    echo '错误：尚未准备 Hermes；请先运行 bash scripts/setup_deps.sh。' >&2
    exit 1
  fi
  mkdir -p "$DEPS_DIR"
  HERMES_TEMP="$(mktemp -d "$DEPS_DIR/hermes-fetch.XXXXXX")"
  trap 'rm -rf -- "$HERMES_TEMP"' EXIT
  git init -q "$HERMES_TEMP"
  git -C "$HERMES_TEMP" remote add origin https://github.com/NousResearch/hermes-agent.git
  git -C "$HERMES_TEMP" -c http.proxy="$PROXY" fetch --depth 1 origin "$HERMES_COMMIT"
  git -C "$HERMES_TEMP" checkout --detach "$HERMES_COMMIT"
  mv "$HERMES_TEMP" "$HERMES_SRC"
  trap - EXIT
fi
if [[ "$(git -C "$HERMES_SRC" rev-parse HEAD)" != "$HERMES_COMMIT" ]]; then
  echo "错误：已有 Hermes 不是课程固定提交 ${HERMES_COMMIT}。未修改已有目录；请保留本机工作后，在独立目录准备课程依赖。" >&2
  exit 1
fi
if [[ -n "$(git -C "$HERMES_SRC" status --porcelain --untracked-files=no)" ]]; then
  echo '错误：Hermes 有已跟踪文件修改，无法按固定版本核验；未覆盖本机文件。' >&2
  exit 1
fi

VENV="$HERMES_SRC/.venv"
PYTHON="$VENV/bin/python"
if [[ ! -d "$VENV" ]]; then
  if $CHECK_ONLY; then
    echo '错误：Hermes Python 环境尚未创建。' >&2
    exit 1
  fi
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$VENV"
  else
    SYSTEM_PYTHON=""
    for candidate in python3.12 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
        SYSTEM_PYTHON="$candidate"
        break
      fi
    done
    if [[ -z "$SYSTEM_PYTHON" ]]; then
      echo "错误：本仓库统一使用 Python 3.12；请先安装 uv 或兼容的 Python，再重新运行。" >&2
      exit 1
    fi
    "$SYSTEM_PYTHON" -m venv "$VENV"
  fi
else
  echo "复用 .deps/hermes-agent/.venv。"
fi

if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c \
  'import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] < (3, 14)))'; then
  echo "错误：已有 Hermes .venv 不可用，或 Python 不在上游支持的3.11—3.13范围。" >&2
  exit 1
fi

if $CHECK_ONLY; then
  "$PYTHON" -c 'import run_agent, dotenv, sys; print(f"固定 Hermes 提交、Python {sys.version_info.major}.{sys.version_info.minor} 与核心依赖检查通过。")'
  exit 0
fi

# Hermes pyproject.toml 声明核心依赖，上游 setup-hermes.sh 支持 editable 安装。
# 练习使用核心包，不安装开发、桌面、语音等扩展。
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PYTHON" -e "$HERMES_SRC"
else
  # uv 创建的 venv 默认不带 pip；后续改用无 uv 的环境时也能继续安装。
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m ensurepip --upgrade
  fi
  "$PYTHON" -m pip --proxy "$PROXY" install -e "$HERMES_SRC"
fi

echo "准备完成：Hermes 源码已就绪，共用解释器为 .deps/hermes-agent/.venv/bin/python。"
echo "运行练习前，请按第 11、12 讲各自的 README 配置凭据，再执行相应目录的 bash run.sh。"
