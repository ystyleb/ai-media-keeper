#!/usr/bin/env bash
#
# 一键启动 NAS 媒体管家 —— 强制使用项目 .venv 的 Python。
#
# 为什么需要这个脚本:
#   直接 `python3 app.py` 若没先 `source .venv/bin/activate`,会用全局 Python,
#   而全局环境常缺 openai 等依赖 → DeepSeek 测试等功能报 "SDK not installed"。
#   本脚本永远用 .venv/bin/python,并在缺依赖时自动补装,杜绝这类环境错配。
#
# 用法:
#   ./start.sh              前台启动(Ctrl+C 停止,可看首启 token 与实时日志)
#   ./start.sh -d           后台启动(nohup,日志写 logs/app.log)
#   ./start.sh stop         停止后台服务
#   ./start.sh restart      后台重启
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PY="$SCRIPT_DIR/.venv/bin/python"
VENV_PIP="$SCRIPT_DIR/.venv/bin/pip"
PORT=8080
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/app.log"
PID_FILE="$SCRIPT_DIR/.venv/nas-app.pid"   # 放 .venv 内,已被 .gitignore 忽略

log() { printf '\033[36m[start]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[start]\033[0m %s\n' "$*" >&2; }

# 输出监听 $PORT 的 PID(无则空);| true 防 set -e 在 lsof 无匹配时退出
port_pid() {
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

stop_service() {
    local pid
    pid="$(port_pid)"
    if [[ -z "$pid" ]]; then
        log "端口 $PORT 无服务在跑,无需停止。"
        rm -f "$PID_FILE"
        return 0
    fi
    log "停止服务 (PID $pid) …"
    kill "$pid" 2>/dev/null || true        # SIGTERM 优雅退出:后台 worker 有状态机可恢复
    for _ in 1 2 3 4 5 6; do
        kill -0 "$pid" 2>/dev/null && sleep 1 || break
    done
    if kill -0 "$pid" 2>/dev/null; then
        err "优雅退出超时,强制 SIGKILL。"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    log "已停止。"
}

ensure_venv() {
    if [[ ! -x "$VENV_PY" ]]; then
        log "未找到 .venv,正在创建并安装依赖 …"
        python3 -m venv .venv
        "$VENV_PIP" install -q --upgrade pip
        "$VENV_PIP" install -q -r requirements.txt
    fi
    # 校验关键依赖(openai 是这次踩坑的那个);缺则按 requirements 补装
    if ! "$VENV_PY" -c "import openai, flask, requests, apscheduler, guessit, mcp" 2>/dev/null; then
        log "依赖不完整,正在按 requirements.txt 补装 …"
        "$VENV_PIP" install -q -r requirements.txt
    fi
}

start_service() {
    local daemon="$1"
    ensure_venv

    local running_pid
    running_pid="$(port_pid)"
    if [[ -n "$running_pid" ]]; then
        err "端口 $PORT 已被占用 (PID $running_pid),服务可能已在运行。"
        err "如需重启:  ./start.sh restart"
        exit 1
    fi

    log "使用 $("$VENV_PY" --version 2>&1)  @  $VENV_PY"
    if [[ "$daemon" == "1" ]]; then
        mkdir -p "$LOG_DIR"
        nohup "$VENV_PY" app.py > "$LOG_FILE" 2>&1 < /dev/null &
        local pid=$!
        echo "$pid" > "$PID_FILE"
        sleep 2
        if kill -0 "$pid" 2>/dev/null; then
            log "后台启动成功 (PID $pid)。"
            log "日志:  tail -f $LOG_FILE"
            log "停止:  ./start.sh stop"
        else
            err "启动失败,日志末尾:"
            tail -n 15 "$LOG_FILE" >&2 || true
            exit 1
        fi
    else
        log "前台启动(Ctrl+C 停止) …"
        exec "$VENV_PY" app.py
    fi
}

case "${1:-}" in
    stop | --stop)             stop_service ;;
    restart | --restart)       stop_service; start_service 1 ;;
    -d | --daemon | daemon)    start_service 1 ;;
    "" | -f | --foreground)    start_service 0 ;;
    -h | --help)
        sed -n '2,15p' "$0" | sed 's/^#\s\{0,1\}//'
        ;;
    *)
        err "未知参数: $1"
        err "用法: ./start.sh [ -d | stop | restart ]"
        exit 1
        ;;
esac
