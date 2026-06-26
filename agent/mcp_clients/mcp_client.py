"""
OpsAgent — MCP Client
=====================
A resilient HTTP client for communicating with MCP servers.
Implements:
  • Automatic tool discovery (GET /mcp/tools/list)
  • Tool execution (POST /mcp/tools/call)
  • JWT authentication (fetches scoped token from auth service)
  • Exponential back-off retries via tenacity
  • Circuit-breaker pattern via pybreaker
"""

import asyncio
import logging
import os
import threading
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
from pybreaker import CircuitBreaker, CircuitBreakerError

logger = logging.getLogger("opsagent.mcp_client")

# ---------------------------------------------------------------------------
# Background event loop (safe async-to-sync bridge)
# ---------------------------------------------------------------------------
# We run a dedicated event loop in a daemon thread. This avoids the
# deadlock / RuntimeError that occurs when calling asyncio.run() or
# loop.run_until_complete() from within an already-running event loop
# (which is the case inside FastAPI / uvicorn).

_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_thread: threading.Thread | None = None
_bg_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    """Lazily create a background event loop running in a daemon thread."""
    global _bg_loop, _bg_thread
    if _bg_loop is not None and _bg_loop.is_running():
        return _bg_loop
    with _bg_lock:
        # Double-check after acquiring lock
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        _bg_loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_bg_loop)
            _bg_loop.run_forever()

        _bg_thread = threading.Thread(target=_run, daemon=True, name="mcp-async-bridge")
        _bg_thread.start()
        return _bg_loop


def _run_async(coro) -> Any:
    """Schedule a coroutine on the background loop and wait for the result."""
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


# ---------------------------------------------------------------------------
# Circuit breaker — opens after 3 consecutive failures, resets after 30 s
# ---------------------------------------------------------------------------

_breakers: dict[str, CircuitBreaker] = {}


def _get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            fail_max=3,
            reset_timeout=30,
            name=name,
        )
    return _breakers[name]


# ---------------------------------------------------------------------------
# JWT Token Cache
# ---------------------------------------------------------------------------

_token_cache: dict[str, tuple[str, float]] = {}  # scope -> (token, expires_at)


def _get_cached_token(scope: str) -> str | None:
    """Return a valid cached token, or None if expired/absent."""
    if scope in _token_cache:
        token, expires_at = _token_cache[scope]
        if time.time() < expires_at - 30:  # 30s buffer before expiry
            return token
    return None


async def _fetch_jwt(auth_url: str, scope: str) -> str | None:
    """Fetch a fresh JWT from the auth service."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{auth_url}/auth/token",
                json={"client_id": "opsagent", "scope": scope},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data["access_token"]
            ttl = data.get("expires_in", 300)
            _token_cache[scope] = (token, time.time() + ttl)
            logger.debug("[%s] JWT fetched, expires in %ds", scope, ttl)
            return token
    except Exception as exc:
        logger.warning("Failed to fetch JWT for scope %s: %s", scope, exc)
        return None


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------

class MCPClient:
    """Stateless, resilient client for a single MCP server."""

    def __init__(self, name: str, base_url: str, timeout: float = 10.0,
                 auth_url: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._breaker = _get_breaker(name)
        self._tools: list[dict] | None = None
        self._auth_url = auth_url or os.getenv("AUTH_URL", "http://auth:8000")

    # -- JWT helper ----------------------------------------------------------

    async def _get_auth_headers(self) -> dict[str, str]:
        """Get Authorization header with a valid JWT."""
        # Try cache first
        token = _get_cached_token(self.name)
        if not token:
            token = await _fetch_jwt(self._auth_url, self.name)
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    # -- Tool discovery ------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def discover_tools(self) -> list[dict]:
        """Fetch the tool registry from the MCP server."""
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/mcp/tools/list", headers=headers)
            resp.raise_for_status()
            self._tools = resp.json()
            logger.info("[%s] Discovered %d tools", self.name, len(self._tools))
            return self._tools

    def get_cached_tools(self) -> list[dict]:
        """Return previously discovered tools (no network call)."""
        return self._tools or []

    # -- Tool execution (async) ----------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _call_raw(self, tool: str, arguments: dict) -> dict:
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/mcp/tools/call",
                json={"tool": tool, "arguments": arguments},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def call_tool(self, tool: str, arguments: dict | None = None) -> dict[str, Any]:
        """
        Execute a tool on this MCP server (async version).
        Wraps the HTTP call with circuit-breaker + retry logic.
        """
        arguments = arguments or {}
        try:
            # pybreaker's call() expects a sync callable.  We bridge via
            # _run_async so the breaker can track success/failure properly.
            result = self._breaker.call(
                _run_async, self._call_raw(tool, arguments)
            )
            logger.info("[%s] Tool %s returned successfully", self.name, tool)
            return result
        except CircuitBreakerError:
            logger.error("[%s] Circuit OPEN — skipping tool %s", self.name, tool)
            return {
                "tool": tool,
                "result": {"error": f"Circuit breaker open for {self.name} — service degraded"},
                "timestamp": "",
                "latency_ms": 0,
            }

    def call_tool_sync(self, tool: str, arguments: dict | None = None) -> dict[str, Any]:
        """
        Synchronous wrapper for call_tool().
        Safe to call from synchronous LangGraph nodes or any sync context.
        """
        arguments = arguments or {}
        try:
            result = self._breaker.call(
                _run_async, self._call_raw(tool, arguments)
            )
            logger.info("[%s] Tool %s returned successfully (sync)", self.name, tool)
            return result
        except CircuitBreakerError:
            logger.error("[%s] Circuit OPEN — skipping tool %s", self.name, tool)
            return {
                "tool": tool,
                "result": {"error": f"Circuit breaker open for {self.name} — service degraded"},
                "timestamp": "",
                "latency_ms": 0,
            }

    # -- Health check --------------------------------------------------------

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.json()
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc)}
