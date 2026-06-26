"""
Tests — LangGraph Workflow
===========================
Unit tests for the workflow module, testing individual nodes in isolation.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow import triage, AgentState


class TestTriageNode:
    def test_triage_assigns_incident_id(self):
        state: AgentState = {
            "alert": {"type": "OOM", "service": "checkout-service"},
            "incident_id": "",
            "service": "",
            "k8s_data": {},
            "prom_data": {},
            "github_data": {},
            "rca_report": "",
            "jira_ticket": {},
            "error": "",
        }
        result = triage(state)
        assert result["incident_id"].startswith("INC-")
        assert len(result["incident_id"]) == 12  # INC- + 8 hex chars

    def test_triage_extracts_service(self):
        state: AgentState = {
            "alert": {"type": "HIGH_CPU", "service": "api-server"},
            "incident_id": "",
            "service": "",
            "k8s_data": {},
            "prom_data": {},
            "github_data": {},
            "rca_report": "",
            "jira_ticket": {},
            "error": "",
        }
        result = triage(state)
        assert result["service"] == "api-server"

    def test_triage_defaults_unknown_service(self):
        state: AgentState = {
            "alert": {"type": "OOM"},
            "incident_id": "",
            "service": "",
            "k8s_data": {},
            "prom_data": {},
            "github_data": {},
            "rca_report": "",
            "jira_ticket": {},
            "error": "",
        }
        result = triage(state)
        assert result["service"] == "unknown"

    def test_triage_preserves_alert_data(self):
        alert = {"type": "POD_CRASH", "service": "my-svc", "description": "Pod crashed"}
        state: AgentState = {
            "alert": alert,
            "incident_id": "",
            "service": "",
            "k8s_data": {},
            "prom_data": {},
            "github_data": {},
            "rca_report": "",
            "jira_ticket": {},
            "error": "",
        }
        result = triage(state)
        assert result["alert"] == alert

    def test_triage_unique_ids(self):
        """Each triage call should generate a unique incident ID."""
        state: AgentState = {
            "alert": {"type": "OOM", "service": "svc"},
            "incident_id": "",
            "service": "",
            "k8s_data": {},
            "prom_data": {},
            "github_data": {},
            "rca_report": "",
            "jira_ticket": {},
            "error": "",
        }
        ids = set()
        for _ in range(50):
            result = triage(state)
            ids.add(result["incident_id"])
        assert len(ids) == 50  # All unique
