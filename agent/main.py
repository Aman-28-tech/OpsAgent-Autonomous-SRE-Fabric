"""
OpsAgent — FastAPI Application
================================
The central orchestrator that receives alert webhooks, dispatches the
LangGraph agent workflow, and exposes health / metrics endpoints.

Production features:
  • Groq LLM for free, blazing-fast inference (Llama 3.3 70B)
  • Prometheus-format /metrics via prometheus_fastapi_instrumentator
  • RabbitMQ queue publishing for alert processing at scale
  • JWT-authenticated MCP calls via the auth service
  • Jira ticket auto-creation from RCA reports
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge

from models import AlertPayload
from workflow import run_agent_workflow
from mcp_clients import MCPClient
import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [AGENT] %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("opsagent")

# ---------------------------------------------------------------------------
# Prometheus metrics (real Prometheus counters/histograms)
# ---------------------------------------------------------------------------

ALERTS_RECEIVED = Counter(
    "opsagent_alerts_received_total",
    "Total number of alerts received by OpsAgent",
)
RCA_COMPLETED = Counter(
    "opsagent_rca_completed_total",
    "Total number of successful RCA workflows",
)
RCA_FAILED = Counter(
    "opsagent_rca_failed_total",
    "Total number of failed RCA workflows",
)
WORKFLOW_DURATION = Histogram(
    "opsagent_workflow_duration_seconds",
    "Duration of the agent workflow in seconds",
    buckets=[1, 2, 5, 10, 20, 30, 60, 120],
)
QUEUE_DEPTH = Gauge(
    "opsagent_queue_depth",
    "Current number of pending alerts in the RabbitMQ queue",
)
JIRA_TICKETS = Counter(
    "opsagent_jira_tickets_created_total",
    "Total number of Jira tickets auto-created from RCA",
)

# ---------------------------------------------------------------------------
# In-memory store for completed RCAs (production: use Redis / Postgres)
# ---------------------------------------------------------------------------

rca_store: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Legacy metrics dict (for backward-compat JSON endpoint)
# ---------------------------------------------------------------------------

legacy_metrics = {
    "alerts_received_total": 0,
    "rca_completed_total": 0,
    "rca_failed_total": 0,
    "agent_step_duration_seconds": [],
}

# ---------------------------------------------------------------------------
# RabbitMQ publisher
# ---------------------------------------------------------------------------

_rabbitmq_channel = None


def _get_rabbitmq_channel():
    """Lazily create a RabbitMQ connection and channel."""
    global _rabbitmq_channel
    if _rabbitmq_channel is not None and _rabbitmq_channel.is_open:
        return _rabbitmq_channel
    try:
        import pika
        params = pika.URLParameters(config.RABBITMQ_URL)
        connection = pika.BlockingConnection(params)
        _rabbitmq_channel = connection.channel()
        _rabbitmq_channel.queue_declare(queue=config.ALERT_QUEUE_NAME, durable=True)
        logger.info("Connected to RabbitMQ, queue='%s'", config.ALERT_QUEUE_NAME)
        return _rabbitmq_channel
    except Exception as exc:
        logger.warning("RabbitMQ unavailable: %s — falling back to in-process", exc)
        return None


def _publish_to_queue(alert_dict: dict) -> bool:
    """Publish an alert to RabbitMQ. Returns True on success."""
    try:
        import pika
        channel = _get_rabbitmq_channel()
        if channel is None:
            return False
        channel.basic_publish(
            exchange="",
            routing_key=config.ALERT_QUEUE_NAME,
            body=json.dumps(alert_dict),
            properties=pika.BasicProperties(
                delivery_mode=2,  # Persistent
                content_type="application/json",
            ),
        )
        logger.info("Alert published to queue '%s'", config.ALERT_QUEUE_NAME)
        return True
    except Exception as exc:
        logger.warning("Failed to publish to RabbitMQ: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("OpsAgent starting — LLM=%s queue=%s", config.LLM_MODEL, config.USE_QUEUE)
    yield
    logger.info("OpsAgent shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpsAgent — Autonomous SRE Fabric",
    version="2.0.0",
    description=(
        "An autonomous Site Reliability Engineer that uses the Model Context "
        "Protocol (MCP) to securely query Kubernetes, Prometheus, and GitHub, "
        "then synthesises a Root Cause Analysis report and auto-creates Jira tickets. "
        "Production-ready with JWT auth, RabbitMQ queue, and Prometheus metrics."
    ),
    lifespan=lifespan,
)

# -- Prometheus instrumentation (exposes /metrics/prometheus) ----------------
Instrumentator().instrument(app).expose(app, endpoint="/metrics/prometheus")


# -- Middleware: request timing ----------------------------------------------

@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Process-Time-Ms"] = f"{elapsed * 1000:.1f}"
    return response


# -- Routes ------------------------------------------------------------------

@app.post("/alert", summary="Receive an alert webhook", tags=["Alerts"])
async def trigger_alert(alert: AlertPayload, background_tasks: BackgroundTasks):
    """
    Accept an incoming infrastructure alert (PagerDuty / Opsgenie style).

    If RabbitMQ is available and USE_QUEUE=true, the alert is published to the
    queue for async processing by a dedicated consumer. Otherwise, falls back
    to in-process background task execution.
    """
    ALERTS_RECEIVED.inc()
    legacy_metrics["alerts_received_total"] += 1
    logger.info("Alert received: type=%s service=%s", alert.type, alert.service)

    alert_dict = alert.model_dump()

    # Try queue-based processing first
    if config.USE_QUEUE and _publish_to_queue(alert_dict):
        return {
            "status": "queued",
            "message": "Alert published to RabbitMQ. Consumer will process it.",
            "queue": config.ALERT_QUEUE_NAME,
            "alert": alert_dict,
        }

    # Fallback: in-process background task
    def _run(alert_dict: dict):
        try:
            start = time.perf_counter()
            result = run_agent_workflow(alert_dict)
            elapsed = time.perf_counter() - start
            WORKFLOW_DURATION.observe(elapsed)
            legacy_metrics["agent_step_duration_seconds"].append(elapsed)
            incident_id = result.get("incident_id", "UNKNOWN")
            rca_store[incident_id] = result
            RCA_COMPLETED.inc()
            legacy_metrics["rca_completed_total"] += 1
            # Track Jira tickets
            if result.get("jira_ticket", {}).get("ticket_key"):
                JIRA_TICKETS.inc()
            logger.info("RCA complete: incident=%s in %.1fs", incident_id, elapsed)
        except Exception as exc:
            RCA_FAILED.inc()
            legacy_metrics["rca_failed_total"] += 1
            logger.exception("Workflow failed: %s", exc)

    background_tasks.add_task(_run, alert_dict)
    return {
        "status": "accepted",
        "message": "Alert processing started (in-process). Check /rca/{incident_id} when ready.",
        "alert": alert_dict,
    }


@app.get("/rca/{incident_id}", summary="Get RCA report by incident ID", tags=["Reports"])
async def get_rca(incident_id: str):
    """Retrieve a completed RCA report."""
    report = rca_store.get(incident_id)
    if not report:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found or still processing.")
    return report


@app.get("/rca", summary="List all completed RCAs", tags=["Reports"])
async def list_rcas():
    """List all incident IDs with completed RCA reports."""
    return {
        "total": len(rca_store),
        "incidents": list(rca_store.keys()),
    }


@app.get("/health", summary="Health check", tags=["Ops"])
async def health():
    return {"status": "healthy", "model": config.LLM_MODEL, "queue_enabled": config.USE_QUEUE}


@app.get("/metrics", summary="Agent self-monitoring metrics (JSON)", tags=["Ops"])
async def agent_metrics():
    """
    JSON metrics for the agent itself.
    For Prometheus-format metrics, use /metrics/prometheus.
    """
    durations = legacy_metrics["agent_step_duration_seconds"]
    return {
        "alerts_received_total": legacy_metrics["alerts_received_total"],
        "rca_completed_total": legacy_metrics["rca_completed_total"],
        "rca_failed_total": legacy_metrics["rca_failed_total"],
        "avg_workflow_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
        "max_workflow_duration_seconds": round(max(durations), 2) if durations else 0,
        "queue_enabled": config.USE_QUEUE,
    }


@app.get("/mcp/status", summary="MCP server connectivity check", tags=["Ops"])
async def mcp_status():
    """Check reachability of all connected MCP servers."""
    k8s = MCPClient("k8s-mcp", config.K8S_MCP_URL)
    prom = MCPClient("prom-mcp", config.PROM_MCP_URL)
    gh = MCPClient("github-mcp", config.GITHUB_MCP_URL)
    jira = MCPClient("jira-mcp", config.JIRA_MCP_URL)
    return {
        "k8s": await k8s.health(),
        "prometheus": await prom.health(),
        "github": await gh.health(),
        "jira": await jira.health(),
    }

# ---------------------------------------------------------------------------
# Web Dashboard (beautiful UI for screenshots & demos)
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/dashboard", summary="Visual SRE Command Center", tags=["Dashboard"],
         response_class=HTMLResponse)
async def dashboard():
    """Serve the OpsAgent visual dashboard."""
    html_file = _STATIC_DIR / "dashboard.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")
    return HTMLResponse(content=html_file.read_text(), status_code=200)


@app.get("/auth/demo-token", summary="Demo JWT token for dashboard", tags=["Dashboard"])
async def demo_jwt_token():
    """Generate a demo JWT token for the dashboard display."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{config.AUTH_URL}/auth/token",
                json={"client_id": "opsagent-dashboard", "scope": "k8s-mcp"},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return {"error": "Auth service unreachable"}
