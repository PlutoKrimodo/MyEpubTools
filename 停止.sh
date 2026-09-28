#!/usr/bin/env bash
# EPUB 工具箱 · 停止服务脚本
#
# 与 启动.sh 配对：按端口定位服务进程并优雅停止。
#
# 用法：
#   ./停止.sh              # 停止默认端口 5000 上的服务
#   ./停止.sh 5001         # 停止指定端口
#   ./停止.sh --status     # 只看有没有在跑，不做任何停止动作
#   ./停止.sh --force      # 跳过「只停本项目 app.py」的安全检查
#
# 也可用环境变量覆盖端口：EPUB_TOOLS_PORT

# 被 `sh 停止.sh` 调用时切回 bash。
if [ -z "${BASH_VERSION:-}" ]; then
    exec bash "$0" "$@"
fi

set -euo pipefail

DEFAULT_PORT=5000
PORT="${EPUB_TOOLS_PORT:-$DEFAULT_PORT}"
MODE="stop"
FORCE=0
TIMEOUT=10

usage() {
    # 打印 shebang 之后、第一行非注释代码之前的所有注释行；
    # 比写死行号稳妥，以后增删注释都不会错位。
    awk 'NR > 1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print; next } exit }' "$0"
}

for arg in "$@"; do
    case "$arg" in
        -f | --force) FORCE=1 ;;
        -s | --status) MODE="status" ;;
        -h | --help) usage; exit 0 ;;
        "") ;;
        *[!0-9]*)
            printf '[错误] 无法识别的参数：%s（用 --help 看用法）\n' "$arg" >&2
            exit 1
            ;;
        *) PORT="$arg" ;;
    esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    printf '[错误] 端口不合法：%s（应为 1-65535 的整数）\n' "$PORT" >&2
    exit 1
fi

info() { printf '%s\n' "$*"; }

# 找出监听指定端口的 PID。
# 本机（Debian 容器）没有 ss，所以先用 netstat；macOS 上 netstat 不支持 -p，回落到 lsof。
port_pids() {
    local pids=""

    if command -v netstat >/dev/null 2>&1; then
        pids=$(netstat -ltnp 2>/dev/null |
            awk -v needle=":$PORT " 'index($0, needle) { print $NF }' |
            cut -d/ -f1 |
            grep -E '^[0-9]+$' || true)
    fi

    if [ -z "$pids" ] && command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    fi

    printf '%s\n' "$pids" | grep -E '^[0-9]+$' | sort -u || true
}

describe() {
    local pid="$1" cmd
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
    [ -n "$cmd" ] || cmd="（进程信息不可读）"
    printf '  PID %s  %s\n' "$pid" "$cmd"
}

PIDS=$(port_pids)

if [ -z "$PIDS" ]; then
    info "端口 $PORT 上没有正在运行的服务。"
    exit 0
fi

# 只停本项目：确认命令行里确实是 app.py，避免误杀占用同一端口的其它程序。
ALLOWED=""
BLOCKED=""
for pid in $PIDS; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if [ "$FORCE" -eq 1 ] || printf '%s' "$cmd" | grep -q 'app\.py'; then
        ALLOWED="$ALLOWED $pid"
    else
        BLOCKED="$BLOCKED $pid"
    fi
done

if [ -n "$BLOCKED" ]; then
    info "端口 $PORT 上这些进程不是本项目的 app.py，已跳过："
    for pid in $BLOCKED; do describe "$pid"; done
    info "确认要停止它们，请加 --force 重跑。"
fi

if [ -z "$ALLOWED" ]; then
    exit 1
fi

if [ "$MODE" = "status" ]; then
    info "端口 $PORT 上的服务正在运行："
    for pid in $ALLOWED; do describe "$pid"; done
    info "（--status 只查看，未做任何停止动作）"
    exit 0
fi

info "正在停止服务（端口 $PORT）……"
for pid in $ALLOWED; do describe "$pid"; done
# shellcheck disable=SC2086
kill $ALLOWED 2>/dev/null || true

for _ in $(seq 1 "$TIMEOUT"); do
    remaining=$(port_pids)
    if [ -z "$remaining" ]; then
        info "已停止。重新启动：./启动.sh"
        exit 0
    fi
    sleep 1
done

# 优雅停止没生效时才强杀——这是个无状态的开发服务器，强杀不会丢数据。
info "进程未在 ${TIMEOUT} 秒内退出，改用强制终止。"
# shellcheck disable=SC2046
kill -9 $(port_pids) 2>/dev/null || true
sleep 1

if [ -z "$(port_pids)" ]; then
    info "已强制停止。重新启动：./启动.sh"
else
    printf '[错误] 端口 %s 仍被占用，请手动检查。\n' "$PORT" >&2
    exit 1
fi
