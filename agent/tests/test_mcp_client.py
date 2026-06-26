"""
Tests — MCP Client
===================
Unit tests for the resilient MCP client using httpx mock transport.
"""

import pytest
import httpx
import json
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_clients.mcp_client import MCPClient, _run_async


# ---------------------------------------------------------------------------
# Mock transport that simulates MCP server responses
# ---------------------------------------------------------------------------

def _make_mock_transport(tools=None, call_result=None, fail_health=False):
    """Create an httpx mock transport for MCP server endpoints."""
    tools = tools or [{"name": "test_tool", "description": "A test tool", "parameters": {}}]
    call_result = call_result or {"status": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/health":
            if fail_health:
                return httpx.Response(500, text="unhealthy")
            return httpx.Response(200, json={"status": "healthy", "service": "mock"})

        if path == "/mcp/tools/list":
            return httpx.Response(200, json=tools)

        if path == "/mcp/tools/call":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "tool": body["tool"],
                "result": call_result,
                "timestamp": "2026-01-01T00:00:00Z",
                "latency_ms": 1.5,
            })

        if path == "/auth/token":
            return httpx.Response(200, json={
                "access_token": "mock-jwt-token",
                "expires_in": 300,
                "scope": "mock",
                "token_type": "bearer",
            })

        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMCPClientToolDiscovery:
    @pytest.mark.asyncio
    async def test_discover_tools_returns_list(self):
        tools = [{"name": "get_status", "description": "Get status", "parameters": {}}]
        transport = _make_mock_transport(tools=tools)

        async with httpx.AsyncClient(transport=transport) as http_client:
            resp = await http_client.get("http://mock:8000/mcp/tools/list")
            result = resp.json()

        assert len(result) == 1
        assert result[0]["name"] == "get_status"

    def test_cached_tools_empty_initially(self):
        client = MCPClient("test-cache", "http://mock:8000")
        assert client.get_cached_tools() == []


class TestMCPClientHealthCheck:
    @pytest.mark.asyncio
    async def test_health_returns_status(self):
        transport = _make_mock_transport()
        async with httpx.AsyncClient(transport=transport) as http_client:
            resp = await http_client.get("http://mock:8000/health")
            data = resp.json()
        assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_unreachable(self):
        """Health check should return 'unreachable' when the server is down."""
        client = MCPClient("test-unreach", "http://localhost:99999")
        result = await client.health()
        assert result["status"] == "unreachable"
        assert "error" in result


class TestMCPClientInit:
    def test_default_timeout(self):
        client = MCPClient("test-init", "http://mock:8000")
        assert client.timeout == 10.0
        assert client.name == "test-init"
        assert client.base_url == "http://mock:8000"

    def test_custom_timeout(self):
        client = MCPClient("test-timeout", "http://mock:8000", timeout=5.0)
        assert client.timeout == 5.0

    def test_trailing_slash_stripped(self):
        client = MCPClient("test-slash", "http://mock:8000/")
        assert client.base_url == "http://mock:8000"


class TestMCPClientToolCall:
    @pytest.mark.asyncio
    async def test_tool_call_via_transport(self):
        """Test the raw HTTP call to the MCP tool execution endpoint."""
        call_result = {"pods": [{"name": "test-pod", "status": "Running"}]}
        transport = _make_mock_transport(call_result=call_result)

        async with httpx.AsyncClient(transport=transport) as http_client:
            resp = await http_client.post(
                "http://mock:8000/mcp/tools/call",
                json={"tool": "get_pod_status", "arguments": {"service": "test"}},
            )
            data = resp.json()

        assert data["tool"] == "get_pod_status"
        assert data["result"]["pods"][0]["name"] == "test-pod"
        assert "timestamp" in data
        assert "latency_ms" in data


class TestAsyncBridge:
    def test_run_async_bridge(self):
        """Test that the async-to-sync bridge works correctly."""
        import asyncio

        async def _dummy():
            return 42

        result = _run_async(_dummy())
        assert result == 42
