"""
OpsAgent — JWT Auth Service
============================
Centralized authentication service that issues short-lived JWTs scoped
per MCP server.  Each MCP server validates the token before executing
any tool call, ensuring a security boundary between the agent and the
data layer.

Endpoints
---------
  POST /auth/token   — Issue a JWT for a specific MCP scope
  GET  /health       — Service health check
  GET  /auth/verify  — Verify a JWT (used by MCP middleware)
"""

import os
import time
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
import jwt as pyjwt
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
JWT_TOKENS_ISSUED = Counter(
    "opsagent_jwt_tokens_issued_total",
    "Total JWT tokens issued by the auth service",
    ["scope"],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "300"))  # 5 min default
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [AUTH] %(levelname)s %(message)s")
logger = logging.getLogger("auth")

# Valid scopes — one per MCP server
VALID_SCOPES = {"k8s-mcp", "prom-mcp", "github-mcp", "jira-mcp"}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    """Request body for token issuance."""
    client_id: str = Field(description="Identifier of the requesting client (e.g., 'opsagent')")
    scope: str = Field(description="Target MCP server scope (e.g., 'k8s-mcp')")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scope: str


class VerifyRequest(BaseModel):
    token: str
    required_scope: str = ""


class VerifyResponse(BaseModel):
    valid: bool
    client_id: str = ""
    scope: str = ""
    expires_at: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpsAgent Auth Service",
    version="1.0.0",
    description="JWT authentication service for MCP server access control",
)


@app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
def issue_token(request: TokenRequest):
    """
    Issue a short-lived JWT scoped to a specific MCP server.
    The agent must request a fresh token before each MCP call.
    """
    if request.scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope: '{request.scope}'. Valid scopes: {sorted(VALID_SCOPES)}"
        )

    now = int(time.time())
    payload = {
        "sub": request.client_id,
        "scope": request.scope,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
        "iss": "opsagent-auth",
    }

    token = pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    JWT_TOKENS_ISSUED.labels(scope=request.scope).inc()
    logger.info("Token issued: client=%s scope=%s ttl=%ds", request.client_id, request.scope, JWT_TTL_SECONDS)

    return TokenResponse(
        access_token=token,
        expires_in=JWT_TTL_SECONDS,
        scope=request.scope,
    )


@app.post("/auth/verify", response_model=VerifyResponse, tags=["Auth"])
def verify_token(request: VerifyRequest):
    """
    Verify a JWT and optionally check its scope.
    Used by MCP servers as middleware validation.
    """
    try:
        decoded = pyjwt.decode(request.token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        return VerifyResponse(valid=False, error="Token expired")
    except pyjwt.InvalidTokenError as exc:
        return VerifyResponse(valid=False, error=f"Invalid token: {exc}")

    # Scope check
    if request.required_scope and decoded.get("scope") != request.required_scope:
        return VerifyResponse(
            valid=False,
            error=f"Scope mismatch: token has '{decoded.get('scope')}', required '{request.required_scope}'"
        )

    return VerifyResponse(
        valid=True,
        client_id=decoded.get("sub", ""),
        scope=decoded.get("scope", ""),
        expires_at=datetime.fromtimestamp(decoded["exp"], tz=timezone.utc).isoformat(),
    )


@app.get("/health", tags=["Ops"])
def health():
    return {"status": "healthy", "service": "auth", "jwt_ttl_seconds": JWT_TTL_SECONDS}


@app.get("/metrics", tags=["Ops"])
def metrics():
    """Prometheus-format metrics for Grafana scraping."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
