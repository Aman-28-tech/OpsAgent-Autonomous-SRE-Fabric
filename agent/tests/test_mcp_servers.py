"""
Tests — MCP Servers
=====================
Unit tests for the K8s MCP server.
Uses importlib with a unique module name to avoid conflicts with other
MCP servers that also have a file called 'server.py'.
"""

import pytest
import importlib.util
import sys
import os


def _load_server_module(name: str, server_path: str):
    """Load a server.py file as a uniquely-named module to avoid collisions."""
    spec = importlib.util.spec_from_file_location(name, server_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── K8s MCP ──────────────────────────────────────────────────────────

K8S_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "mcp", "k8s", "server.py"
)


class TestK8sMCP:
    @pytest.fixture(autouse=True)
    def setup(self):
        from fastapi.testclient import TestClient
        k8s_mod = _load_server_module("k8s_server", K8S_SERVER_PATH)
        self.client = TestClient(k8s_mod.app)

    def test_health(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "k8s-mcp"

    def test_list_tools(self):
        resp = self.client.get("/mcp/tools/list")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) >= 4
        tool_names = [t["name"] for t in tools]
        assert "get_pod_status" in tool_names
        assert "fetch_container_logs" in tool_names
        assert "restart_deployment" in tool_names
        assert "list_pods" in tool_names

    def test_get_pod_status(self):
        resp = self.client.post("/mcp/tools/call", json={
            "tool": "get_pod_status",
            "arguments": {"service": "checkout-service"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["tool"] == "get_pod_status"
        assert "name" in data["result"]
        assert data["result"]["status"] == "CrashLoopBackOff"

    def test_fetch_container_logs(self):
        resp = self.client.post("/mcp/tools/call", json={
            "tool": "fetch_container_logs",
            "arguments": {"service": "checkout-service"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "log_lines" in data["result"]
        assert len(data["result"]["log_lines"]) > 0

    def test_list_pods(self):
        resp = self.client.post("/mcp/tools/call", json={
            "tool": "list_pods",
            "arguments": {},
        })
        assert resp.status_code == 200
        assert "pods" in resp.json()["result"]

    def test_unknown_tool(self):
        resp = self.client.post("/mcp/tools/call", json={
            "tool": "nonexistent",
            "arguments": {},
        })
        assert resp.status_code == 404

    def test_unknown_service(self):
        resp = self.client.post("/mcp/tools/call", json={
            "tool": "get_pod_status",
            "arguments": {"service": "nonexistent-service"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data["result"]
