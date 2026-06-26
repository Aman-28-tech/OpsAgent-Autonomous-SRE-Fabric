"""
Tests — Auth Service
=====================
Unit tests for the JWT auth service.
"""

import pytest
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth"))

from fastapi.testclient import TestClient
from server import app


client = TestClient(app)


class TestHealthEndpoint:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "auth"
        assert "jwt_ttl_seconds" in data


class TestTokenIssuance:
    def test_issue_valid_token(self):
        resp = client.post("/auth/token", json={
            "client_id": "opsagent",
            "scope": "k8s-mcp",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["scope"] == "k8s-mcp"
        assert data["expires_in"] > 0

    def test_issue_token_all_scopes(self):
        """All valid MCP scopes should produce tokens."""
        for scope in ["k8s-mcp", "prom-mcp", "github-mcp", "jira-mcp"]:
            resp = client.post("/auth/token", json={
                "client_id": "test",
                "scope": scope,
            })
            assert resp.status_code == 200
            assert resp.json()["scope"] == scope

    def test_reject_invalid_scope(self):
        resp = client.post("/auth/token", json={
            "client_id": "opsagent",
            "scope": "invalid-scope",
        })
        assert resp.status_code == 400


class TestTokenVerification:
    def test_verify_valid_token(self):
        # First issue a token
        issue_resp = client.post("/auth/token", json={
            "client_id": "test-verifier",
            "scope": "k8s-mcp",
        })
        token = issue_resp.json()["access_token"]

        # Then verify it
        verify_resp = client.post("/auth/verify", json={
            "token": token,
            "required_scope": "k8s-mcp",
        })
        assert verify_resp.status_code == 200
        data = verify_resp.json()
        assert data["valid"] is True
        assert data["client_id"] == "test-verifier"
        assert data["scope"] == "k8s-mcp"

    def test_verify_scope_mismatch(self):
        # Issue token for k8s-mcp
        issue_resp = client.post("/auth/token", json={
            "client_id": "test",
            "scope": "k8s-mcp",
        })
        token = issue_resp.json()["access_token"]

        # Verify with wrong scope
        verify_resp = client.post("/auth/verify", json={
            "token": token,
            "required_scope": "github-mcp",
        })
        data = verify_resp.json()
        assert data["valid"] is False
        assert "mismatch" in data["error"].lower()

    def test_verify_invalid_token(self):
        verify_resp = client.post("/auth/verify", json={
            "token": "not-a-valid-jwt",
        })
        data = verify_resp.json()
        assert data["valid"] is False
