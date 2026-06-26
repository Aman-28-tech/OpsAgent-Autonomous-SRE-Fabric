.PHONY: build up down logs health alert test eval clean scale-consumers

# ──────────────────────────────────────────────────────────────────
# Docker
# ──────────────────────────────────────────────────────────────────
build:
	docker compose build --parallel

up: build
	docker compose up -d
	@echo "Services starting... run 'make health' to check."
	@echo ""
	@echo "📊 Grafana:    http://localhost:3000  (admin / opsagent)"
	@echo "📈 Prometheus: http://localhost:9090"
	@echo "🐰 RabbitMQ:   http://localhost:15672 (guest / guest)"

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=50

# ──────────────────────────────────────────────────────────────────
# Operations
# ──────────────────────────────────────────────────────────────────
health:
	@curl -sf http://localhost:8000/health | python3 -m json.tool
	@curl -sf http://localhost:8001/health | python3 -m json.tool
	@curl -sf http://localhost:8002/health | python3 -m json.tool
	@curl -sf http://localhost:8003/health | python3 -m json.tool
	@curl -sf http://localhost:8004/health | python3 -m json.tool
	@curl -sf http://localhost:8010/health | python3 -m json.tool

alert:
	curl -X POST http://localhost:8000/alert \
		-H "Content-Type: application/json" \
		-d '{"type":"OOM","service":"checkout-service","description":"Memory exceeded limit"}'

metrics:
	@echo "=== JSON Metrics ==="
	@curl -sf http://localhost:8000/metrics | python3 -m json.tool
	@echo ""
	@echo "=== Prometheus Metrics ==="
	@curl -sf http://localhost:8000/metrics/prometheus | head -30

mcp-tools:
	@echo "=== K8s MCP ===" && curl -sf http://localhost:8001/mcp/tools/list | python3 -m json.tool
	@echo "=== Prometheus MCP ===" && curl -sf http://localhost:8002/mcp/tools/list | python3 -m json.tool
	@echo "=== GitHub MCP ===" && curl -sf http://localhost:8003/mcp/tools/list | python3 -m json.tool
	@echo "=== Jira MCP ===" && curl -sf http://localhost:8004/mcp/tools/list | python3 -m json.tool

# -- Auth -------------------------------------------------------------------
token:
	@curl -sf -X POST http://localhost:8010/auth/token \
		-H "Content-Type: application/json" \
		-d '{"client_id":"opsagent","scope":"k8s-mcp"}' | python3 -m json.tool

# -- Scale ------------------------------------------------------------------
scale-consumers:
	docker compose up -d --scale consumer=3
	@echo "Scaled to 3 queue consumers"

# ──────────────────────────────────────────────────────────────────
# Testing
# ──────────────────────────────────────────────────────────────────
test:
	cd agent && python -m pytest tests/ -v --tb=short

eval:
	python evaluation/eval.py --threshold 0.90

# ──────────────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────────────
clean: down
	docker system prune -f
