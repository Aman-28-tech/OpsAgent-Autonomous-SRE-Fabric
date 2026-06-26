"""
Kubernetes MCP Server
=====================
An isolated Model Context Protocol server that exposes Kubernetes cluster
operations as discoverable tools. In production this connects to a real
Kind / minikube cluster via the K8s Python client. For the demo it returns
realistic mock data so the full pipeline can run without infrastructure.

Live Mode
---------
  Set SANDBOX_MODE=live to connect to a real Kubernetes cluster via
  kubeconfig (KUBECONFIG env var or ~/.kube/config).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "mock")  # "mock" | "live"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [K8S-MCP] %(levelname)s %(message)s")
logger = logging.getLogger("k8s-mcp")

# ---------------------------------------------------------------------------
# Live K8s client (loaded only when SANDBOX_MODE=live)
# ---------------------------------------------------------------------------
_k8s_v1 = None
_k8s_apps_v1 = None

if SANDBOX_MODE == "live":
    try:
        from kubernetes import client, config as k8s_config
        # Try in-cluster first, fall back to kubeconfig
        try:
            k8s_config.load_incluster_config()
            logger.info("Loaded in-cluster K8s config")
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            logger.info("Loaded kubeconfig from %s", os.getenv("KUBECONFIG", "~/.kube/config"))
        _k8s_v1 = client.CoreV1Api()
        _k8s_apps_v1 = client.AppsV1Api()
    except Exception as exc:
        logger.error("Failed to initialize K8s client: %s — falling back to mock", exc)
        SANDBOX_MODE = "mock"


# ---------------------------------------------------------------------------
# JWT Validation Middleware
# ---------------------------------------------------------------------------

import jwt as pyjwt

async def validate_jwt(request: Request):
    """Validate JWT on /mcp/* endpoints."""
    if request.url.path.startswith("/mcp/"):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("No JWT provided — allowing in dev mode")
            return
        token = auth_header.split(" ", 1)[1]
        try:
            decoded = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if decoded.get("scope") != "k8s-mcp":
                logger.warning("JWT scope mismatch: %s", decoded.get("scope"))
        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT expired")
        except pyjwt.InvalidTokenError as exc:
            logger.warning("JWT invalid: %s", exc)

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
# Mock data (simulates a real cluster state for demo / CI)
# ---------------------------------------------------------------------------

MOCK_PODS = {
    "checkout-service": {
        "name": "checkout-service-7b9d6f4c8-xk2lp",
        "namespace": "default",
        "status": "CrashLoopBackOff",
        "restart_count": 14,
        "reason": "OOMKilled",
        "last_state": "Terminated",
        "exit_code": 137,
        "started_at": "2026-06-18T09:50:00Z",
        "finished_at": "2026-06-18T10:00:12Z",
        "node": "opsagent-demo-worker",
        "cpu_request": "250m",
        "memory_request": "256Mi",
        "memory_limit": "512Mi",
    },
    "payment-gateway": {
        "name": "payment-gateway-5c8f9a2d1-mv7qz",
        "namespace": "default",
        "status": "Running",
        "restart_count": 0,
        "reason": None,
        "last_state": "Running",
        "exit_code": 0,
        "started_at": "2026-06-17T08:00:00Z",
        "finished_at": None,
        "node": "opsagent-demo-worker",
        "cpu_request": "500m",
        "memory_request": "512Mi",
        "memory_limit": "1Gi",
    },
    "api-server": {
        "name": "api-server-3d7e1b9a0-zt5hw",
        "namespace": "default",
        "status": "CrashLoopBackOff",
        "restart_count": 7,
        "reason": "Error",
        "last_state": "Terminated",
        "exit_code": 1,
        "started_at": "2026-06-18T11:00:00Z",
        "finished_at": "2026-06-18T11:05:30Z",
        "node": "opsagent-demo-worker",
        "cpu_request": "100m",
        "memory_request": "128Mi",
        "memory_limit": "256Mi",
    },
}

MOCK_LOGS = {
    "checkout-service": [
        "2026-06-18T09:55:00Z INFO  Starting checkout-service v2.4.1",
        "2026-06-18T09:55:01Z INFO  Connected to PostgreSQL at db:5432",
        "2026-06-18T09:55:02Z INFO  Loading cart session cache into memory...",
        "2026-06-18T09:56:30Z WARN  Heap usage at 78% (400Mi / 512Mi)",
        "2026-06-18T09:58:00Z WARN  Heap usage at 91% (466Mi / 512Mi) — GC pressure increasing",
        "2026-06-18T09:59:45Z ERROR java.lang.OutOfMemoryError: Java heap space",
        "2026-06-18T09:59:45Z ERROR   at com.shop.cart.SessionCache.loadAll(SessionCache.java:142)",
        "2026-06-18T09:59:45Z ERROR   at com.shop.cart.CartService.init(CartService.java:87)",
        "2026-06-18T09:59:46Z FATAL Container killed by OOM (exit code 137)",
    ],
    "api-server": [
        "2026-06-18T11:00:00Z INFO  Starting api-server v1.8.0",
        "2026-06-18T11:00:01Z INFO  Connecting to Redis at redis:6379",
        "2026-06-18T11:00:02Z ERROR ConnectionRefusedError: [Errno 111] Connection refused",
        "2026-06-18T11:00:02Z ERROR Failed to connect to Redis after 3 retries",
        "2026-06-18T11:00:03Z FATAL Unrecoverable error — shutting down",
    ],
}


# ---------------------------------------------------------------------------
# Live K8s implementations
# ---------------------------------------------------------------------------

def _live_get_pod_status(service: str, namespace: str = "default") -> dict:
    """Fetch real pod status from the K8s cluster."""
    try:
        pods = _k8s_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={service}",
        )
        if not pods.items:
            return {"error": f"No pods found for service '{service}' in namespace '{namespace}'"}

        pod = pods.items[0]
        container = pod.status.container_statuses[0] if pod.status.container_statuses else None

        result = {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": pod.status.phase,
            "restart_count": container.restart_count if container else 0,
            "reason": None,
            "last_state": "Unknown",
            "exit_code": 0,
            "started_at": pod.status.start_time.isoformat() if pod.status.start_time else None,
            "finished_at": None,
            "node": pod.spec.node_name,
            "cpu_request": "",
            "memory_request": "",
            "memory_limit": "",
        }

        if container:
            # Extract state info
            if container.state.waiting:
                result["status"] = container.state.waiting.reason or "Waiting"
                result["reason"] = container.state.waiting.reason
            elif container.state.terminated:
                result["status"] = "Terminated"
                result["reason"] = container.state.terminated.reason
                result["exit_code"] = container.state.terminated.exit_code
                result["finished_at"] = container.state.terminated.finished_at.isoformat() if container.state.terminated.finished_at else None

            # Last terminated state
            if container.last_state and container.last_state.terminated:
                result["last_state"] = "Terminated"
                result["reason"] = container.last_state.terminated.reason or result["reason"]
                result["exit_code"] = container.last_state.terminated.exit_code

            # Resource requests/limits
            resources = pod.spec.containers[0].resources
            if resources.requests:
                result["cpu_request"] = resources.requests.get("cpu", "")
                result["memory_request"] = resources.requests.get("memory", "")
            if resources.limits:
                result["memory_limit"] = resources.limits.get("memory", "")

        return result
    except Exception as exc:
        logger.error("Live get_pod_status failed: %s", exc)
        return {"error": str(exc)}


def _live_fetch_container_logs(service: str, namespace: str = "default", tail_lines: int = 50) -> dict:
    """Fetch real container logs from the K8s cluster."""
    try:
        pods = _k8s_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={service}",
        )
        if not pods.items:
            return {"error": f"No pods found for service '{service}'"}

        pod_name = pods.items[0].metadata.name
        logs = _k8s_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            previous=True,  # Get logs from the crashed container
        )
        log_lines = logs.strip().split("\n") if logs else []
        return {"service": service, "pod": pod_name, "log_lines": log_lines, "total_lines": len(log_lines)}
    except Exception as exc:
        # If previous container logs fail, try current
        try:
            logs = _k8s_v1.read_namespaced_pod_log(
                name=pods.items[0].metadata.name,
                namespace=namespace,
                tail_lines=tail_lines,
            )
            log_lines = logs.strip().split("\n") if logs else []
            return {"service": service, "log_lines": log_lines, "total_lines": len(log_lines)}
        except Exception as inner_exc:
            logger.error("Live fetch_container_logs failed: %s", inner_exc)
            return {"error": str(inner_exc)}


def _live_restart_deployment(service: str, namespace: str = "default") -> dict:
    """Perform a rolling restart by patching the deployment's annotation."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                        }
                    }
                }
            }
        }
        _k8s_apps_v1.patch_namespaced_deployment(
            name=service,
            namespace=namespace,
            body=body,
        )
        return {"status": "success", "message": f"Rolling restart initiated for deployment/{service}", "timestamp": now}
    except Exception as exc:
        logger.error("Live restart_deployment failed: %s", exc)
        return {"error": str(exc)}


def _live_list_pods(namespace: str = "default") -> dict:
    """List all pods in the cluster."""
    try:
        pods = _k8s_v1.list_namespaced_pod(namespace=namespace)
        return {
            "pods": [
                {
                    "name": p.metadata.name,
                    "status": p.status.phase,
                    "restarts": sum(
                        cs.restart_count for cs in (p.status.container_statuses or [])
                    ),
                }
                for p in pods.items
            ]
        }
    except Exception as exc:
        logger.error("Live list_pods failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Kubernetes MCP Server",
    version="1.0.0",
    description="Model Context Protocol server for Kubernetes cluster operations",
)


@app.middleware("http")
async def jwt_middleware(request: Request, call_next):
    await validate_jwt(request)
    return await call_next(request)


# -- Tool registry ----------------------------------------------------------

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_pod_status",
        description="Get the status, restart count, exit code, and resource limits of a pod by service name.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the Kubernetes service / deployment"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="fetch_container_logs",
        description="Fetch the most recent container logs for a given service. Returns the last N log lines.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the Kubernetes service / deployment"},
                "tail_lines": {"type": "integer", "default": 50},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="restart_deployment",
        description="Perform a rolling restart of a deployment by service name.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Name of the deployment to restart"},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="list_pods",
        description="List all pods across the cluster with their current status.",
        parameters={
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "default": "default"},
            },
        },
    ),
]


# -- Tool implementations (dispatch based on mode) -------------------------

def _get_pod_status(service: str, **kwargs) -> dict:
    if SANDBOX_MODE == "live" and _k8s_v1:
        return _live_get_pod_status(service, **kwargs)
    pod = MOCK_PODS.get(service)
    if not pod:
        return {"error": f"Pod for service '{service}' not found", "available": list(MOCK_PODS.keys())}
    return pod


def _fetch_container_logs(service: str, tail_lines: int = 50, **kwargs) -> dict:
    if SANDBOX_MODE == "live" and _k8s_v1:
        return _live_fetch_container_logs(service, tail_lines=tail_lines, **kwargs)
    logs = MOCK_LOGS.get(service)
    if not logs:
        return {"error": f"No logs found for service '{service}'", "available": list(MOCK_LOGS.keys())}
    return {"service": service, "log_lines": logs[-tail_lines:], "total_lines": len(logs)}


def _restart_deployment(service: str, **kwargs) -> dict:
    if SANDBOX_MODE == "live" and _k8s_apps_v1:
        return _live_restart_deployment(service, **kwargs)
    if service not in MOCK_PODS:
        return {"error": f"Deployment '{service}' not found"}
    return {"status": "success", "message": f"Rolling restart initiated for deployment/{service}", "timestamp": datetime.now(timezone.utc).isoformat()}


def _list_pods(**kwargs) -> dict:
    if SANDBOX_MODE == "live" and _k8s_v1:
        return _live_list_pods(**kwargs)
    return {
        "pods": [
            {"name": v["name"], "status": v["status"], "restarts": v["restart_count"]}
            for v in MOCK_PODS.values()
        ]
    }


TOOL_DISPATCH = {
    "get_pod_status": _get_pod_status,
    "fetch_container_logs": _fetch_container_logs,
    "restart_deployment": _restart_deployment,
    "list_pods": _list_pods,
}


# -- MCP endpoints ----------------------------------------------------------

@app.get("/mcp/tools/list", response_model=list[ToolDefinition])
def list_tools():
    """MCP tool discovery — returns every tool this server exposes."""
    return TOOLS


@app.post("/mcp/tools/call", response_model=ToolCallResponse)
def call_tool(request: ToolCallRequest):
    """MCP tool execution — runs the requested tool and returns the result."""
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
    logger.info("Tool %s executed in %.1f ms (mode=%s)", request.tool, elapsed, SANDBOX_MODE)
    return ToolCallResponse(tool=request.tool, result=result, timestamp=start.isoformat(), latency_ms=round(elapsed, 2))


@app.get("/health")
def health():
    return {"status": "healthy", "service": "k8s-mcp", "mode": SANDBOX_MODE}
