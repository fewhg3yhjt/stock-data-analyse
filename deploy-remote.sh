#!/bin/bash
# ====================================================
# deploy-remote.sh — 通用服务器部署脚本
#
# 用法（服务器上执行）:
#   bash deploy-remote.sh <project> <package.tar.gz> [--dir /opt/xxx]
#     project: StockInvestmentTool | personal_blog
#     package: xxx-code.tar.gz（云端构建） 或 xxx-deploy.tar.gz（镜像包）
#
# 自动完成:
#   解压 → 数据保护(.env/output 保留) → 加载镜像(镜像包) → 启动 → 健康检查
# ====================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $*"; }

if [ $# -lt 2 ]; then
    echo "用法: bash $0 <project> <package.tar.gz> [--dir /opt/xxx]"
    exit 1
fi
PROJECT="$1"
PACKAGE="$(readlink -f "$2")"
[ -f "${PACKAGE}" ] || { log_error "找不到部署包: $2"; exit 1; }

# 项目默认部署目录
case "${PROJECT}" in
    StockInvestmentTool) APP_DIR="${APP_DIR:-/opt/stock_data_analyse}" ;;
    personal_blog)       APP_DIR="${APP_DIR:-/opt/personal-blog}" ;;
    *) log_error "未知项目: ${PROJECT}"; exit 1 ;;
esac
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir) APP_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

command -v docker >/dev/null || { log_error "Docker 未安装！curl -fsSL https://get.docker.com | bash"; exit 1; }
command -v docker compose >/dev/null || { log_error "Docker Compose 未安装"; exit 1; }

# ── 基础镜像国内镜像站 fallback ────────────────
# 服务器首次云端构建需拉基础镜像（FROM xxx），国内访问 Docker Hub 会超时。
# 自动从可用国内镜像站拉取并 tag，绕开 registry-mirrors 机制（新版 Docker 可能已移除）。
base_image_of() {
    [ -f "$1/Dockerfile" ] || return 1
    grep -iE "^FROM " "$1/Dockerfile" | head -1 | awk '{print $2}'
}
ensure_base_image() {
    local base="$1"
    docker image inspect "$base" >/dev/null 2>&1 && { log_ok "基础镜像已存在: $base"; return 0; }
    log_info "基础镜像 $base 不存在，尝试国内镜像站..."
    local repo="${base%:*}" tag="${base##*:}"
    for m in docker.m.daocloud.io docker.1ms.run dockerproxy.com docker.rainbond.cc dockerpull.com; do
        local full="${m}/library/${repo}:${tag}"
        log_info "  → ${full}"
        if docker pull "${full}" 2>/dev/null; then
            docker tag "${full}" "${base}"
            docker rmi "${full}" 2>/dev/null || true
            log_ok "  基础镜像就绪（${m}）"
            return 0
        fi
        log_warn "    失败，换下一个..."
    done
    log_warn "  镜像站均失败，构建将尝试直连 Docker Hub（可能超时，可手动 docker pull 国内镜像站后 tag）"
}

mkdir -p "${APP_DIR}"
cd "${APP_DIR}"
tar xzf "${PACKAGE}" -C "${APP_DIR}"

# 数据保护
log_info "数据保护检查"
if [ -f ".env" ]; then
    log_ok "✅ .env 已存在 — 保留"
else
    if [ -f ".env.example" ]; then
        log_warn "⚠️  .env 不存在，从模板创建（请编辑填入配置后重启）"
        cp .env.example .env
    else
        log_error ".env 与 .env.example 都不存在"; exit 1
    fi
fi
if [ -d "output" ] && [ "$(ls -A output 2>/dev/null)" ]; then
    log_ok "✅ output/ 已有数据 — 保留"
else
    log_info "📁 output/ 为空或不存在 — 自动创建"
fi

# 镜像包 → 加载镜像；代码包 → 云端构建
if [ -f "${PROJECT}-image.tar.gz" ]; then
    OLD_ID=$(docker images --format "{{.ID}}" "${PROJECT}-image:latest" 2>/dev/null || echo "")
    gzip -dc "${PROJECT}-image.tar.gz" | docker load
    NEW_ID=$(docker images --format "{{.ID}}" "${PROJECT}-image:latest" 2>/dev/null || echo "")
    if [ -n "${OLD_ID}" ] && [ "${OLD_ID}" != "${NEW_ID}" ]; then
        docker rmi "${OLD_ID}" 2>/dev/null || true
    fi
    log_ok "镜像加载完成"
fi

log_info "启动服务..."
docker compose down 2>/dev/null || true
# 云端构建前确保基础镜像（国内镜像站 fallback；镜像包模式无 Dockerfile 则跳过）
if BASE_IMAGE="$(base_image_of "${APP_DIR}")" && [ -n "${BASE_IMAGE}" ]; then
    ensure_base_image "${BASE_IMAGE}"
fi
export STOCK_ENV_FILE="${APP_DIR}/.env"
export STOCK_OUTPUT_DIR="${APP_DIR}/output"
mkdir -p "${APP_DIR}/output"
if [ -f "${PROJECT}-image.tar.gz" ]; then
    docker compose up -d
else
    docker compose up -d --build   # 代码包 → 云端构建
fi
log_ok "容器已启动"

# 健康检查
CONTAINER=$(grep 'container_name:' docker-compose.yml 2>/dev/null | head -1 | awk '{print $2}')
CONTAINER="${CONTAINER:-stock-invest}"
log_info "健康检查（最多 90 秒）..."
HEALTHY=false
for i in $(seq 1 9); do
    sleep 10
    docker ps --format "{{.Names}}" | grep -q "${CONTAINER}" || { log_warn "  尝试 ${i}/9: 容器未运行"; continue; }
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${CONTAINER}" 2>/dev/null || echo "no-health")
    if [ "${STATUS}" = "healthy" ]; then HEALTHY=true; break; fi
done
if [ "${HEALTHY}" = true ]; then
    log_ok "✅ 部署成功"
else
    log_warn "⚠️  服务已启动但健康检查未通过，日志: docker compose logs --tail=30"
fi
echo ""
log_ok "日志: docker compose logs -f  |  重启: docker compose restart"
