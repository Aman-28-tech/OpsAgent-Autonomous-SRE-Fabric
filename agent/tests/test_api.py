"""
Tests — FastAPI routes
======================
Uses httpx + pytest to test the Agent API endpoints.
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

# We need sys.path workaround because the agent dir is not a proper package
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Ensure USE_QUEUE is False so alert tests get predictable "accepted" status
# instead of trying to connect to RabbitMQ and returning "queued".
os.environ["USE_QUEUE"] = "false"

from main import app


client = TestClient(app)


class TestHealthEndpoint:
    def test_health_returns_200(self):
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_body_structure(self):
        data = client.get("/health").json()
        assert "status" in data
        assert data["status"] == "healthy"
        assert "model" in data
        assert "queue_enabled" in data


class TestMetricsEndpoint:
    def test_metrics_returns_200(self):
        response = client.get("/metrics")
        assert response.status_code == 200

    def test_metrics_counters(self):
        data = client.get("/metrics").json()
        assert "alerts_received_total" in data
        assert "rca_completed_total" in data
        assert "rca_failed_total" in data
        assert "avg_workflow_duration_seconds" in data
        assert "queue_enabled" in data


class TestAlertEndpoint:
    def test_alert_accepts_valid_payload(self):
        payload = {
            "type": "OOM",
            "service": "checkout-service",
            "description": "Memory exceeded limit",
        }
        response = client.post("/alert", json=payload)
        assert response.status_code == 200
        data = response.json()
        # With USE_QUEUE=false, we always get "accepted" status
        assert data["status"] == "accepted"

    def test_alert_accepts_all_types(self):
        """Verify every AlertType enum value is accepted."""
        for alert_type in ["OOM", "HIGH_CPU", "HIGH_ERROR_RATE", "POD_CRASH", "DEPENDENCY_FAILURE"]:
            response = client.post("/alert", json={
                "type": alert_type,
                "service": "test-service",
            })
            assert response.status_code == 200, f"Failed for type: {alert_type}"

    def test_alert_rejects_invalid_type(self):
        payload = {
            "type": "INVALID_TYPE",
            "service": "checkout-service",
        }
        response = client.post("/alert", json=payload)
        assert response.status_code == 422  # Validation error

    def test_alert_rejects_missing_service(self):
        payload = {"type": "OOM"}
        response = client.post("/alert", json=payload)
        assert response.status_code == 422

    def test_alert_response_contains_alert_data(self):
        payload = {
            "type": "HIGH_CPU",
            "service": "api-server",
            "description": "CPU over 90%",
        }
        response = client.post("/alert", json=payload)
        data = response.json()
        assert data["alert"]["type"] == "HIGH_CPU"
        assert data["alert"]["service"] == "api-server"


class TestRCAEndpoint:
    def test_rca_not_found(self):
        response = client.get("/rca/NONEXISTENT")
        assert response.status_code == 404

    def test_list_rcas(self):
        response = client.get("/rca")
        assert response.status_code == 200
        data = response.json()
        assert "total" in data
        assert "incidents" in data
        assert isinstance(data["incidents"], list)


class TestTimingMiddleware:
    def test_response_has_timing_header(self):
        response = client.get("/health")
        assert "X-Process-Time-Ms" in response.headers
        # Verify it's a valid number
        timing = float(response.headers["X-Process-Time-Ms"])
        assert timing >= 0
