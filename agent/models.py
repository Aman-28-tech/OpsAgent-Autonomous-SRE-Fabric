"""
OpsAgent — Pydantic Models
===========================
Shared request / response schemas for the Agent API.
"""

from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field


class AlertType(str, Enum):
    OOM = "OOM"
    HIGH_CPU = "HIGH_CPU"
    HIGH_ERROR_RATE = "HIGH_ERROR_RATE"
    POD_CRASH = "POD_CRASH"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"


class AlertPayload(BaseModel):
    """Incoming alert webhook payload (mirrors PagerDuty / Opsgenie style)."""
    type: AlertType
    service: str
    description: str = ""
    severity: str = "critical"
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class RCAReport(BaseModel):
    """Structured Root Cause Analysis output."""
    incident_id: str
    service: str
    alert_type: str
    symptoms: str
    timeline: str
    root_cause: str
    remediation: str
    confidence: float = Field(ge=0, le=1, description="Agent confidence 0-1")
    sources: list[str] = Field(default_factory=list, description="MCP servers consulted")
    raw_markdown: str = Field(default="", description="Full markdown RCA report")
    jira_ticket_key: str = Field(default="", description="Auto-created Jira ticket key")
    jira_ticket_url: str = Field(default="", description="URL to the Jira ticket")


class WorkflowState(BaseModel):
    """Internal state carried through the LangGraph pipeline."""
    alert: dict = Field(default_factory=dict)
    incident_id: str = ""
    service: str = ""
    k8s_data: dict = Field(default_factory=dict)
    prom_data: dict = Field(default_factory=dict)
    github_data: dict = Field(default_factory=dict)
    rca_report: str = ""
    jira_ticket: dict = Field(default_factory=dict)
    error: str = ""
