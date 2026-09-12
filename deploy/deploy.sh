#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  个人驾驶舱 · 一键部署脚本 (VPS / Docker)
#  用法: chmod +x deploy.sh && ./deploy.sh [init|up|down|logs]
# ═══════════════════════════════════════════════════════════════
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="$SCRIPT_DIR"
DATA_DIR="$SCRIPT_DIR/data"
CONFIG_FILE="$SCRIPT_DIR/.env"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}" >&2; }

cmd_init() {
    log "初始化部署环境..."

    # 数据目录
    mkdir -p "$DATA_DIR"/{情感账户,倾听笔记,影响圈,角色}
    mkdir -p "$DATA_DIR"/logs
    mkdir -p "$SCRIPT_DIR/certs"    # SSL（可选）

    # 凭据模板
    if [ ! -f "$CONFIG_FILE" ]; then
        cp "$DEPLOY_DIR/.env.example" "$CONFIG_FILE"
        ok "已生成 $CONFIG_FILE，请填入真实凭据"
    else
        ok "配置文件已存在: $CONFIG_FILE"
    fi

    # 初始化空 JSON 文件
    for f in \
        "$DATA_DIR/情感账户/relations.json" \
        "$DATA_DIR/倾听笔记/listening.json" \
        "$DATA_DIR/影响圈/proactive.json"; do
        [ -f "$f" ] || echo '[]' > "$f"
    done
    [ -f "$DATA_DIR/habit_dims.json" ] || echo '{}' > "$DATA_DIR/habit_dims.json"

    ok "数据目录就绪: $DATA_DIR"
    log ""
    log "下一步:"
    log "  1. 编辑 $CONFIG_FILE 填入 TickTick Token 和 LLM API Key"
    log "  2. 运行 ./deploy.sh up 启动服务"
    log "  3. 通过你配置的受认证 HTTPS 入口访问（后端仅绑定 127.0.0.1）"
}

cmd_up() {
    log "构建并启动..."
    cd "$SCRIPT_DIR"
    docker compose -f "$DEPLOY_DIR/docker-compose.yml" build --quiet
    docker compose -f "$DEPLOY_DIR/docker-compose.yml" up -d
    sleep 3
    if curl -sf -m 5 http://127.0.0.1:8787/api/chain-health > /dev/null 2>&1; then
        ok "后端健康检查通过 ✓"
    else
        warn "后端尚未响应（可能还在启动中），稍后用 ./deploy.sh logs 查看"
    fi
    log "服务已启动；请通过你配置的受认证 HTTPS 入口访问。"
}

cmd_down() {
    log "停止并清理..."
    cd "$SCRIPT_DIR"
    docker compose -f "$DEPLOY_DIR/docker-compose.yml" down
    ok "已停止"
}

cmd_logs() {
    cd "$SCRIPT_DIR"
    docker compose -f "$DEPLOY_DIR/docker-compose.yml" logs -f --tail 50 dashboard 2>/dev/null || err "容器未运行"
}

case "${1:-help}" in
    init) cmd_init ;;
    up)   cmd_up ;;
    down) cmd_down ;;
    logs) cmd_logs ;;
    help|*)
        echo "个人驾驶舱部署工具"
        echo ""
        echo "用法: $0 <命令>"
        echo ""
        echo "命令:"
        echo "  init   初始化数据目录和配置模板"
        echo "  up     构建镜像并启动服务"
        echo "  down   停止并清理容器"
        echo "  logs   查看实时日志"
        echo "  help   显示此帮助"
        ;;
esac
