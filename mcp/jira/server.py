"""
Jira Ticketing MCP Server
==========================
An isolated Model Context Protocol server that exposes Jira operations
as discoverable tools.  In production this connects to Jira Cloud / Server
via the REST API.  For the demo it simulates ticket creation and returns
realistic mock responses so the full pipeline runs without infrastructure.

Tools
-----
  create_ticket    — Create a Jira issue from RCA data
  get_ticket       — Retrieve an existing ticket by key
  list_tickets     — List recent tickets for a service
"""

import os
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "mock")  # "mock" | "live"
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://company.atlassian.net")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "OPS")
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [JIRA-MCP] %(levelname)s %(message)s")
logger = logging.getLogger("jira-mcp")

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
# JWT Validation Middleware
# ---------------------------------------------------------------------------

import jwt as pyjwt

async def validate_jwt(request: Request):
    """Validate JWT on /mcp/* endpoints."""
    if request.url.path.startswith("/mcp/"):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            # Allow unauthenticated in dev; log warning
            logger.warning("No JWT provided — allowing in dev mode")
            return
        token = auth_header.split(" ", 1)[1]
        try:
            decoded = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if decoded.get("scope") != "jira-mcp":
                logger.warning("JWT scope mismatch: %s", decoded.get("scope"))
        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT expired")
        except pyjwt.InvalidTokenError as exc:
            logger.warning("JWT invalid: %s", exc)


# ---------------------------------------------------------------------------
# In-memory ticket store (simulates Jira for demo)
# ---------------------------------------------------------------------------

_ticket_counter = 100
_tickets: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Jira Ticketing MCP Server",
    version="1.0.0",
    description="Model Context Protocol server for Jira ticket management",
)


@app.middleware("http")
async def jwt_middleware(request: Request, call_next):
    await validate_jwt(request)
    return await call_next(request)


# -- Tool registry ----------------------------------------------------------

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="create_ticket",
        description="Create a Jira issue from an RCA report. Returns the ticket key and URL.",
        parameters={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string", "description": "OpsAgent incident ID"},
                "service": {"type": "string", "description": "Affected service name"},
                "summary": {"type": "string", "description": "Ticket title/summary"},
                "description": {"type": "string", "description": "Full RCA report as markdown"},
                "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"], "default": "High"},
                "labels": {"type": "array", "items": {"type": "string"}, "default": ["opsagent", "auto-rca"]},
            },
            "required": ["incident_id", "service", "summary", "description"],
        },
    ),
    ToolDefinition(
        name="get_ticket",
        description="Retrieve a Jira ticket by its key (e.g., OPS-101).",
        parameters={
            "type": "object",
            "properties": {
                "ticket_key": {"type": "string", "description": "Jira ticket key (e.g., OPS-101)"},
            },
            "required": ["ticket_key"],
        },
    ),
    ToolDefinition(
        name="list_tickets",
        description="List recent Jira tickets created by OpsAgent for a given service.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name to filter by"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["service"],
        },
    ),
]


# -- Tool implementations ---------------------------------------------------

def _create_ticket(incident_id: str, service: str, summary: str,
                   description: str, priority: str = "High",
                   labels: list[str] | None = None, **kwargs) -> dict:
    global _ticket_counter
    _ticket_counter += 1
    ticket_key = f"{JIRA_PROJECT_KEY}-{_ticket_counter}"

    ticket = {
        "key": ticket_key,
        "project": JIRA_PROJECT_KEY,
        "summary": summary,
        "description": description,
        "priority": priority,
        "status": "Open",
        "labels": labels or ["opsagent", "auto-rca"],
        "incident_id": incident_id,
        "service": service,
        "assignee": "unassigned",
        "reporter": "opsagent-bot",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": f"{JIRA_BASE_URL}/browse/{ticket_key}",
    }
    _tickets[ticket_key] = ticket
    logger.info("Ticket created: %s for incident %s", ticket_key, incident_id)
    return {
        "status": "created",
        "ticket_key": ticket_key,
        "url": ticket["url"],
        "summary": summary,
        "priority": priority,
    }


def _get_ticket(ticket_key: str, **kwargs) -> dict:
    ticket = _tickets.get(ticket_key)
    if not ticket:
        return {"error": f"Ticket '{ticket_key}' not found", "available": list(_tickets.keys())}
    return ticket


def _list_tickets(service: str, limit: int = 10, **kwargs) -> dict:
    matching = [t for t in _tickets.values() if t.get("service") == service]
    matching.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return {
        "service": service,
        "total": len(matching),
        "tickets": [
            {"key": t["key"], "summary": t["summary"], "status": t["status"], "created_at": t["created_at"]}
            for t in matching[:limit]
        ],
    }


TOOL_DISPATCH = {
    "create_ticket": _create_ticket,
    "get_ticket": _get_ticket,
    "list_tickets": _list_tickets,
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
    logger.info("Tool %s executed in %.1f ms", request.tool, elapsed)
    return ToolCallResponse(tool=request.tool, result=result, timestamp=start.isoformat(), latency_ms=round(elapsed, 2))


@app.get("/health")
def health():
    return {"status": "healthy", "service": "jira-mcp", "mode": SANDBOX_MODE, "tickets_created": len(_tickets)}
