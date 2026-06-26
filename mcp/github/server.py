"""
GitHub MCP Server
=================
An isolated Model Context Protocol server that exposes GitHub repository
operations as discoverable tools. In production this uses the GitHub REST
API with a scoped Personal Access Token. For the demo it returns
realistic mock commit and PR data.
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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [GH-MCP] %(levelname)s %(message)s")
logger = logging.getLogger("github-mcp")

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
# Mock data
# ---------------------------------------------------------------------------

MOCK_COMMITS = {
    "checkout-service": [
        {
            "sha": "a1b2c3d4e5f6",
            "message": "feat: preload all cart sessions into memory on startup for faster checkout",
            "author": "dev-alice@company.com",
            "timestamp": "2026-06-18T09:30:00Z",
            "files_changed": ["src/cart/SessionCache.java", "src/cart/CartService.java"],
            "additions": 47,
            "deletions": 3,
        },
        {
            "sha": "f6e5d4c3b2a1",
            "message": "fix: update PostgreSQL driver to 42.7.1",
            "author": "dev-bob@company.com",
            "timestamp": "2026-06-18T08:00:00Z",
            "files_changed": ["pom.xml"],
            "additions": 1,
            "deletions": 1,
        },
        {
            "sha": "1a2b3c4d5e6f",
            "message": "chore: bump log4j to 2.23.0",
            "author": "dependabot[bot]@github.com",
            "timestamp": "2026-06-17T22:00:00Z",
            "files_changed": ["pom.xml"],
            "additions": 1,
            "deletions": 1,
        },
    ],
    "api-server": [
        {
            "sha": "7g8h9i0j1k2l",
            "message": "refactor: switch from in-process cache to external Redis",
            "author": "dev-charlie@company.com",
            "timestamp": "2026-06-18T10:45:00Z",
            "files_changed": ["src/cache/redis_client.py", "src/config.py", "docker-compose.yml"],
            "additions": 82,
            "deletions": 15,
        },
    ],
}

MOCK_PR_DIFFS = {
    "checkout-service": {
        "pr_number": 247,
        "title": "feat: preload cart sessions for faster checkout",
        "author": "dev-alice@company.com",
        "state": "merged",
        "merged_at": "2026-06-18T09:30:00Z",
        "reviewers": ["dev-bob@company.com"],
        "approval_status": "approved",
        "diff_summary": (
            "--- a/src/cart/SessionCache.java\n"
            "+++ b/src/cart/SessionCache.java\n"
            "@@ -140,3 +140,49 @@\n"
            "+    // PERF: Load ALL active sessions into heap on init\n"
            "+    public void loadAll() {\n"
            "+        List<Session> sessions = db.query(\"SELECT * FROM cart_sessions WHERE active = true\");\n"
            "+        for (Session s : sessions) {\n"
            "+            this.cache.put(s.getId(), s);  // ⚠️ No eviction policy\n"
            "+        }\n"
            "+        log.info(\"Loaded {} sessions into memory\", sessions.size());\n"
            "+    }\n"
        ),
        "risk_analysis": "HIGH — unbounded heap allocation without eviction policy. "
                         "If cart_sessions table grows, this will exceed the 512Mi memory limit.",
    },
    "api-server": {
        "pr_number": 251,
        "title": "refactor: switch to external Redis cache",
        "author": "dev-charlie@company.com",
        "state": "merged",
        "merged_at": "2026-06-18T10:45:00Z",
        "reviewers": [],
        "approval_status": "self-merged (no review)",
        "diff_summary": (
            "--- a/src/config.py\n"
            "+++ b/src/config.py\n"
            "@@ -10,1 +10,2 @@\n"
            "-CACHE_BACKEND = 'memory'\n"
            "+CACHE_BACKEND = 'redis'\n"
            "+REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379')\n"
        ),
        "risk_analysis": "MEDIUM — hard dependency on Redis without connection retry logic. "
                         "If Redis is unavailable at startup the service will crash.",
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
            if decoded.get("scope") != "github-mcp":
                logger.warning("JWT scope mismatch: %s", decoded.get("scope"))
        except pyjwt.ExpiredSignatureError:
            logger.warning("JWT expired")
        except pyjwt.InvalidTokenError as exc:
            logger.warning("JWT invalid: %s", exc)


app = FastAPI(
    title="GitHub MCP Server",
    version="1.0.0",
    description="Model Context Protocol server for GitHub repository interactions",
)


@app.middleware("http")
async def jwt_middleware(request, call_next):
    await validate_jwt(request)
    return await call_next(request)

TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="get_recent_commits",
        description="Fetch the most recent commits for a service repository, including changed files, author, and timestamps.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Repository / service name"},
                "limit": {"type": "integer", "default": 5, "description": "Max commits to return"},
            },
            "required": ["service"],
        },
    ),
    ToolDefinition(
        name="fetch_pr_diff",
        description="Fetch the diff and metadata of the most recently merged Pull Request for a service, including risk analysis.",
        parameters={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Repository / service name"},
            },
            "required": ["service"],
        },
    ),
]


def _get_recent_commits(service: str, limit: int = 5, **kwargs) -> dict:
    commits = MOCK_COMMITS.get(service)
    if not commits:
        return {"error": f"No commits found for '{service}'", "available": list(MOCK_COMMITS.keys())}
    return {"service": service, "commits": commits[:limit], "total_returned": min(limit, len(commits))}


def _fetch_pr_diff(service: str, **kwargs) -> dict:
    diff = MOCK_PR_DIFFS.get(service)
    if not diff:
        return {"error": f"No PR data for '{service}'", "available": list(MOCK_PR_DIFFS.keys())}
    return diff


TOOL_DISPATCH = {
    "get_recent_commits": _get_recent_commits,
    "fetch_pr_diff": _fetch_pr_diff,
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
    return {"status": "healthy", "service": "github-mcp", "mode": SANDBOX_MODE}
