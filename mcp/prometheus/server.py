"""
Prometheus / Metrics MCP Server
================================
An isolated Model Context Protocol server that exposes infrastructure
metrics queries as discoverable tools. In production this connects to a
real Prometheus instance via PromQL HTTP API. For the demo it returns
realistic time-series mock data.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "mock")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [PROM-MCP] %(levelname)s %(message)s")
logger = logging.getLogger("prom-mcp")

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict = Field(default_factory=dict)


class ToolCallRequest(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    tool: str
    result: Any
    timestamp: str
    latency_ms: float


# ---------------------------------------------------------------------------
# Mock metrics data
# ---------------------------------------------------------------------------

MOCK_CPU = {
    "checkout-service": {
        "service": "checkout-service",
        "metric": "container_cpu_usage_seconds_total",
        "time_range": "last_30m",
        "datapoints": [
            {"time": "T-30m", "value_cores": 0.12},
            {"time": "T-25m", "value_cores": 0.15},
            {"time": "T-20m", "value_cores": 0.18},
            {"time": "T-15m", "value_cores": 0.35},
            {"time": "T-10m", "value_cores": 0.72},
            {"time": "T-5m",  "value_cores": 0.95},
            {"time": "T-0m",  "value_cores": 0.0},
        ],
        "average_cores": 0.35,
        "peak_cores": 0.95,
        "current_cores": 0.0,
        "anomaly_detected": True,
        "anomaly_description": "CPU usage spiked 6x above baseline before pod termination",
    },
    "api-server": {
        "service": "api-server",
        "metric": "container_cpu_usage_seconds_total",
        "time_range": "last_30m",
        "datapoints": [
            {"time": "T-30m", "value_cores": 0.05},
            {"time": "T-25m", "value_cores": 0.05},
            {"time": "T-20m", "value_cores": 0.04},
            {"time": "T-15m", "value_cores": 0.06},
            {"time": "T-10m", "value_cores": 0.05},
            {"time": "T-5m",  "value_cores": 0.0},
            {"time": "T-0m",  "value_cores": 0.0},
        ],
        "average_cores": 0.04,
        "peak_cores": 0.06,
        "current_cores": 0.0,
        "anomaly_detected": False,
        "anomaly_description": "CPU usage was normal; process exited due to dependency failure, not resource exhaustion",
    },
}

MOCK_MEMORY = {
    "checkout-service": {
        "service": "checkout-service",
        "metric": "container_memory_working_set_bytes",
        "time_range": "last_30m",
        "memory_limit_mb": 512,
        "datapoints": [
            {"time": "T-30m", "value_mb": 180},
            {"time": "T-25m", "value_mb": 210},
            {"time": "T-20m", "value_mb": 275},
            {"time": "T-15m", "value_mb": 340},
            {"time": "T-10m", "value_mb": 420},
            {"time": "T-5m",  "value_mb": 498},
            {"time": "T-0m",  "value_mb": 512},
        ],
        "average_mb": 348,
        "peak_mb": 512,
        "spike_detected": True,
        "spike_start": "T-20m",
        "spike_description": "Memory grew linearly from 180 Mi to 512 Mi (limit) over 30 minutes — classic memory leak pattern",
    },
    "api-server": {
        "service": "api-server",
        "metric": "container_memory_working_set_bytes",
        "time_range": "last_30m",
        "memory_limit_mb": 256,
        "datapoints": [
            {"time": "T-30m", "value_mb": 64},
            {"time": "T-25m", "value_mb": 65},
            {"time": "T-20m", "value_mb": 63},
            {"time": "T-15m", "value_mb": 66},
            {"time": "T-10m", "value_mb": 60},
            {"time": "T-5m",  "value_mb": 0},
            {"time": "T-0m",  "value_mb": 0},
        ],
        "average_mb": 53,
        "peak_mb": 66,
        "spike_detected": False,
        "spike_description": "Memory usage was well within limits; no leak detected",
    },
}

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# JWT Validation Middleware
# ---------------------------------------------------------------------------

import jwt as pyjwt

async def validate_jwt(request):
    """Validate JWT on /mcp/* endpoints."""
    if request.url.path.startswith("/mcp/"):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("No JWT provided — allowing in dev mode")
            return
        token = auth_header.split(" ", 1)[1]
        try:
            decoded = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if decoded.get("scope") != "prom-mcp":
                logger.warning("JWT scope mismatch: %s", decoded.get("scope"))
        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT expired")
        except pyjwt.InvalidTokenError as exc:
            logger.warning("JWT invalid: %s", exc)


app = FastAPI(
    title="Prometheus MCP Server",
    version="1.0.0",
    description="Model Context Protocol server for infrastructure metrics queries",
)


@app.middleware("http")
async def jwt_middleware(request, call_next):
    await validate_jwt(request)
    return await call_next(request)

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="query_cpu_usage",
        description="Query CPU usage time-series for a service over the last 30 minutes. Returns datapoints, peak, average, and anomaly detection.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the service to query"},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="query_memory_spikes",
        description="Query memory usage time-series for a service. Detects spikes and returns the memory limit, peak, and leak analysis.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the service to query"},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="query_error_rate",
        description="Query the HTTP 5xx error rate for a service over the last 30 minutes.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the service to query"},
            },
            "required": ["service"],
        },
    ),
]


def _query_cpu_usage(service: str, **kwargs) -> dict:
    data = MOCK_CPU.get(service)
    if not data:
        return {"error": f"No CPU metrics for '{service}'", "available": list(MOCK_CPU.keys())}
    return data


def _query_memory_spikes(service: str, **kwargs) -> dict:
    data = MOCK_MEMORY.get(service)
    if not data:
        return {"error": f"No memory metrics for '{service}'", "available": list(MOCK_MEMORY.keys())}
    return data


def _query_error_rate(service: str, **kwargs) -> dict:
    rates = {
        "checkout-service": {"service": "checkout-service", "error_rate_5xx": 0.42, "total_requests_30m": 12400, "errors_30m": 5208, "baseline_error_rate": 0.002},
        "api-server": {"service": "api-server", "error_rate_5xx": 1.0, "total_requests_30m": 890, "errors_30m": 890, "baseline_error_rate": 0.001},
    }
    data = rates.get(service)
    if not data:
        return {"error": f"No error rate data for '{service}'"}
    return data


TOOL_DISPATCH = {
    "query_cpu_usage": _query_cpu_usage,
    "query_memory_spikes": _query_memory_spikes,
    "query_error_rate": _query_error_rate,
}


@app.get("/mcp/tools/list", response_model=list[ToolDefinition])
def list_tools():
    return TOOLS


@app.post("/mcp/tools/call", response_model=ToolCallResponse)
def call_tool(request: ToolCallRequest):
    start = datetime.now(timezone.utc)
    handler = TOOL_DISPATCH.get(request.tool)
    if not handler:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {request.tool}")
    try:
        result = handler(**request.arguments)
    except Exception as exc:
        logger.exception("Tool execution failed: %s", request.tool)
        raise HTTPException(status_code=500, detail=str(exc))
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
    logger.info("Tool %s executed in %.1f ms", request.tool, elapsed)
    return ToolCallResponse(tool=request.tool, result=result, timestamp=start.isoformat(), latency_ms=round(elapsed, 2))


@app.get("/health")
def health():
    return {"status": "healthy", "service": "prom-mcp", "mode": SANDBOX_MODE}
