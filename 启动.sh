#!/usr/bin/env bash
# EPUB 工具箱 · Linux / macOS / WSL 启动脚本
#
# 与 Windows 的 启动.bat 对称：自动准备虚拟环境、安装依赖、启动服务。
#
# 用法：
#   ./启动.sh                 # 自动选择监听地址（容器内 0.0.0.0，普通机器 127.0.0.1）
#   ./启动.sh 0.0.0.0         # 显式指定监听地址
#   ./启动.sh 0.0.0.0 5001    # 再指定端口
#
# 也可用环境变量覆盖：EPUB_TOOLS_HOST / EPUB_TOOLS_PORT
# 想禁止自动打开浏览器：EPUB_TOOLS_NO_BROWSER=1 ./启动.sh

# 被 `sh 启动.sh` 调用时切回 bash，避免 BASH_SOURCE 等特性不可用。
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

info() { printf '%s\n' "$*"; }
fail() {
    printf '\n[错误] %s\n' "$*" >&2
    exit 1
}

# 优先 python3，其次 python；要求 3.7+（代码用到了 dataclasses）。
find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# 容器 / 云开发环境里必须监听 0.0.0.0，端口转发才连得进来；普通机器保持回环更安全。
default_host() {
    if [ -f /.dockerenv ] ||
        [ -n "${CNB_PIPELINE_ID:-}" ] ||
        [ -n "${REMOTE_CONTAINERS:-}" ] ||
        [ -n "${CODESPACES:-}" ]; then
        printf '0.0.0.0'
    else
        printf '127.0.0.1'
    fi
}

open_browser() {
    local url="$1"
    if command -v wslview >/dev/null 2>&1; then
        wslview "$url" >/dev/null 2>&1 &
    elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
        open "$url" >/dev/null 2>&1 &
    fi
}

info "=========================================="
info "      EPUB 工具箱"
info "=========================================="
info ""

# ------------------------------------------------------------------ 解释器
PYTHON_BIN="$(find_python)" ||
    fail '未找到 Python 3.7 或更高版本。请先安装 Python 后再运行本脚本。'
info "解释器：$PYTHON_BIN（$("$PYTHON_BIN" -V 2>&1)）"

# -------------------------------------------------------------- 虚拟环境
# 不往系统 Python 里装包：新版发行版多为 PEP 668「外部托管环境」，
# 直接 pip install 会被拒绝并可能破坏系统包管理。
if [ ! -x "$VENV_PY" ]; then
    info "创建虚拟环境：$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" ||
        fail '创建虚拟环境失败。Debian/Ubuntu 可先安装：sudo apt install python3-venv'
else
    info "虚拟环境已存在：$VENV_DIR"
fi

# ------------------------------------------------------------------ 依赖
# 检查全部依赖，不是只看 flask/bs4：opencc（简繁转换）是在函数内部延迟导入的，
# 缺了不会让应用起不来，只会静默少功能——早期的 .venv 正是这样被误判成
# 「已就绪」，直到点下按钮才在页面里报错。
if "$VENV_PY" -c 'import flask, bs4, opencc' >/dev/null 2>&1; then
    info "依赖已就绪。"
else
    if [ ! -f "$REQUIREMENTS" ]; then
        fail "缺少依赖清单：$REQUIREMENTS"
    fi
    info "安装依赖（首次运行需要联网）……"
    "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    if ! "$VENV_PY" -m pip install --quiet -r "$REQUIREMENTS"; then
        # flask/bs4 缺失时应用根本起不来，必须停机报错；只差延迟导入的包
        # （opencc）时允许继续启动——整台服务不该因为简繁转换用不了就起不来。
        if "$VENV_PY" -c 'import flask, bs4' >/dev/null 2>&1; then
            info "警告：部分依赖未能安装，简繁转换可能不可用。"
        else
            fail '依赖安装失败，请检查网络或 Python 配置。'
        fi
    fi
fi

# ------------------------------------------------------------------ 参数
HOST="${EPUB_TOOLS_HOST:-}"
PORT="${EPUB_TOOLS_PORT:-5000}"

if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
    HOST="$1"
fi
if [ "$#" -ge 2 ] && [ -n "${2:-}" ]; then
    PORT="$2"
fi
[ -n "$HOST" ] || HOST="$(default_host)"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    fail "端口不合法：$PORT（应为 1-65535 的整数）"
fi

# ------------------------------------------------------------------ 启动
info ""
info "监听地址：$HOST:$PORT"
info "本机访问：http://127.0.0.1:$PORT/"
if [ "$HOST" = "0.0.0.0" ]; then
    info "部署在容器 / 云开发环境时，请通过 IDE 的端口转发访问（转发端口 $PORT）。"
fi
info "按 Ctrl+C 停止服务。"
info ""

if [ -z "${EPUB_TOOLS_NO_BROWSER:-}" ]; then
    (sleep 2 && open_browser "http://127.0.0.1:$PORT/") &
fi

EPUB_TOOLS_HOST="$HOST" EPUB_TOOLS_PORT="$PORT" exec "$VENV_PY" "$SCRIPT_DIR/app.py"
