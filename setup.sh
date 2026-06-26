#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# OpsAgent — One-Click Setup & Demo (v2.0 Production)
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║       OpsAgent — Autonomous SRE Fabric  v2.0             ║"
echo "║       Production Stack: Auth + Queue + Monitoring        ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Build
echo -e "${YELLOW}▶ Building Docker images...${NC}"
docker compose build --parallel

# Start
echo -e "${YELLOW}▶ Starting services...${NC}"
docker compose up -d

# Wait for health
echo -e "${YELLOW}▶ Waiting for services to become healthy...${NC}"
sleep 15

# Health check function
check_service() {
    local url="$1"
    local name="$2"
    if curl -sf "$url" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✅ ${name}${NC} — ${url}"
        return 0
    else
        echo -e "  ${RED}❌ ${name}${NC} — ${url}"
        return 1
    fi
}

ALL_HEALTHY=true

echo ""
echo -e "${YELLOW}▶ MCP & Agent services:${NC}"
check_service "http://localhost:8001/health" "K8s MCP" || ALL_HEALTHY=false
check_service "http://localhost:8002/health" "Prometheus MCP" || ALL_HEALTHY=false
check_service "http://localhost:8003/health" "GitHub MCP" || ALL_HEALTHY=false
check_service "http://localhost:8004/health" "Jira MCP" || ALL_HEALTHY=false
check_service "http://localhost:8010/health" "Auth Service" || ALL_HEALTHY=false
check_service "http://localhost:8000/health" "Agent Orchestrator" || ALL_HEALTHY=false

# Check infrastructure services
echo ""
echo -e "${YELLOW}▶ Infrastructure services:${NC}"
for svc_url in "http://localhost:15672" "http://localhost:9090" "http://localhost:3000"; do
    case "$svc_url" in
        *15672*) svc_name="RabbitMQ Management" ;;
        *9090*)  svc_name="Prometheus" ;;
        *3000*)  svc_name="Grafana Dashboard" ;;
    esac
    if curl -sf "$svc_url" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✅ ${svc_name}${NC} — ${svc_url}"
    else
        echo -e "  ${YELLOW}⏳ ${svc_name}${NC} — ${svc_url} (may need a moment)"
    fi
done

if [ "$ALL_HEALTHY" = false ]; then
    echo -e "\n${RED}Some MCP services failed to start. Check: docker compose logs${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  All services running!${NC}"
echo ""
echo -e "  Agent API:       ${CYAN}http://localhost:8000${NC}"
echo -e "  K8s MCP:         ${CYAN}http://localhost:8001${NC}"
echo -e "  Prometheus MCP:  ${CYAN}http://localhost:8002${NC}"
echo -e "  GitHub MCP:      ${CYAN}http://localhost:8003${NC}"
echo -e "  Jira MCP:        ${CYAN}http://localhost:8004${NC}"
echo -e "  Auth Service:    ${CYAN}http://localhost:8010${NC}"
echo ""
echo -e "  📊 Grafana:      ${CYAN}http://localhost:3000${NC}  (admin / opsagent)"
echo -e "  📈 Prometheus:   ${CYAN}http://localhost:9090${NC}"
echo -e "  🐰 RabbitMQ:     ${CYAN}http://localhost:15672${NC} (guest / guest)"
echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}▶ Fire a synthetic OOM alert:${NC}"
echo ""
echo '  curl -X POST http://localhost:8000/alert \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"type":"OOM","service":"checkout-service","description":"Memory exceeded limit"}'"'"''
echo ""
echo -e "${YELLOW}▶ Get a JWT token:${NC}"
echo '  curl -X POST http://localhost:8010/auth/token \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"client_id":"opsagent","scope":"k8s-mcp"}'"'"''
echo ""
echo -e "${YELLOW}▶ View MCP tool discovery:${NC}"
echo "  curl http://localhost:8001/mcp/tools/list | python3 -m json.tool"
echo ""
echo -e "${YELLOW}▶ Check agent metrics:${NC}"
echo "  curl http://localhost:8000/metrics | python3 -m json.tool"
echo ""
echo -e "${YELLOW}▶ Scale consumers:${NC}"
echo "  docker compose up -d --scale consumer=3"
echo ""
