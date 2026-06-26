"""
OpsAgent — LangGraph Workflow
==============================
A production-grade agentic pipeline built on LangGraph's StateGraph.

Nodes
-----
1. triage         – Parse alert, set incident ID
2. query_k8s      – Call K8s MCP → pod status + container logs
3. query_metrics  – Call Prometheus MCP → CPU / memory time-series
4. query_github   – Call GitHub MCP → recent commits + PR diff
5. synthesize_rca – Feed all context to LLM → generate structured RCA
6. create_ticket  – Call Jira MCP → auto-create issue from RCA
7. emit_report    – Format & persist the final report

Each node is instrumented with Langfuse @observe() for end-to-end tracing.
"""

import json
import logging
import uuid
from typing import TypedDict

from langfuse.decorators import observe
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END

from mcp_clients import MCPClient
import config

logger = logging.getLogger("opsagent.workflow")

# ---------------------------------------------------------------------------
# State schema (TypedDict for LangGraph)
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    alert: dict
    incident_id: str
    service: str
    k8s_data: dict
    prom_data: dict
    github_data: dict
    rca_report: str
    jira_ticket: dict
    error: str


# ---------------------------------------------------------------------------
# MCP client singletons
# ---------------------------------------------------------------------------

k8s_client = MCPClient("k8s-mcp", config.K8S_MCP_URL)
prom_client = MCPClient("prom-mcp", config.PROM_MCP_URL)
github_client = MCPClient("github-mcp", config.GITHUB_MCP_URL)
jira_client = MCPClient("jira-mcp", config.JIRA_MCP_URL)


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

@observe(name="triage")
def triage(state: AgentState) -> AgentState:
    """Parse the incoming alert and assign an incident ID."""
    incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
    service = state["alert"].get("service", "unknown")
    logger.info("Triage: incident=%s service=%s type=%s", incident_id, service, state["alert"].get("type"))
    return {**state, "incident_id": incident_id, "service": service}


@observe(name="query_k8s")
def query_k8s(state: AgentState) -> AgentState:
    """Fetch pod status and container logs from the K8s MCP server."""
    service = state["service"]
    try:
        pod_status = k8s_client.call_tool_sync("get_pod_status", {"service": service})
        container_logs = k8s_client.call_tool_sync("fetch_container_logs", {"service": service})
        k8s_data = {
            "pod_status": pod_status.get("result", pod_status),
            "container_logs": container_logs.get("result", container_logs),
        }
        logger.info("K8s data collected for %s", service)
    except Exception as exc:
        logger.error("K8s MCP call failed: %s", exc)
        k8s_data = {"error": str(exc)}
    return {**state, "k8s_data": k8s_data}


@observe(name="query_metrics")
def query_metrics(state: AgentState) -> AgentState:
    """Fetch CPU and memory metrics from the Prometheus MCP server."""
    service = state["service"]
    try:
        cpu = prom_client.call_tool_sync("query_cpu_usage", {"service": service})
        memory = prom_client.call_tool_sync("query_memory_spikes", {"service": service})
        prom_data = {
            "cpu": cpu.get("result", cpu),
            "memory": memory.get("result", memory),
        }
        logger.info("Prometheus data collected for %s", service)
    except Exception as exc:
        logger.error("Prometheus MCP call failed: %s", exc)
        prom_data = {"error": str(exc)}
    return {**state, "prom_data": prom_data}


@observe(name="query_github")
def query_github(state: AgentState) -> AgentState:
    """Fetch recent commits and PR diffs from the GitHub MCP server."""
    service = state["service"]
    try:
        commits = github_client.call_tool_sync("get_recent_commits", {"service": service})
        pr_diff = github_client.call_tool_sync("fetch_pr_diff", {"service": service})
        github_data = {
            "commits": commits.get("result", commits),
            "pr_diff": pr_diff.get("result", pr_diff),
        }
        logger.info("GitHub data collected for %s", service)
    except Exception as exc:
        logger.error("GitHub MCP call failed: %s", exc)
        github_data = {"error": str(exc)}
    return {**state, "github_data": github_data}


RCA_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer (SRE) AI agent. Your job is to
analyse cross-platform telemetry from Kubernetes, Prometheus, and GitHub to
produce a precise, actionable Root Cause Analysis (RCA) report.

Rules:
- Only reference data that was actually provided to you. Never hallucinate logs, SHAs, or metrics.
- Structure the report exactly as: Symptoms, Timeline, Root Cause, Remediation.
- Be specific — cite exact pod names, commit SHAs, memory values, and timestamps.
- Keep the tone professional and concise.
"""


@observe(name="synthesize_rca")
def synthesize_rca(state: AgentState) -> AgentState:
    """Feed all MCP context to the LLM and generate a structured RCA."""
    llm = ChatGroq(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE,
        groq_api_key=config.GROQ_API_KEY,
    )

    user_prompt = f"""
## Incident {state['incident_id']}
**Service:** {state['service']}
**Alert:** {json.dumps(state['alert'], indent=2)}

### Kubernetes Data
```json
{json.dumps(state['k8s_data'], indent=2)}
```

### Prometheus Metrics
```json
{json.dumps(state['prom_data'], indent=2)}
```

### GitHub Activity
```json
{json.dumps(state['github_data'], indent=2)}
```

Generate a complete Root Cause Analysis (RCA) report in Markdown with the
following sections: **Symptoms**, **Timeline**, **Root Cause**, **Remediation**.
"""

    try:
        response = llm.invoke([
            SystemMessage(content=RCA_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])
        rca = response.content
        logger.info("RCA synthesised for incident %s (%d chars)", state["incident_id"], len(rca))
    except Exception as exc:
        logger.error("LLM synthesis failed: %s", exc)
        rca = f"# RCA Generation Failed\n\nError: {exc}"
    return {**state, "rca_report": rca}


@observe(name="create_ticket")
def create_ticket(state: AgentState) -> AgentState:
    """Auto-create a Jira issue from the RCA report via the Jira MCP server."""
    rca = state.get("rca_report", "")
    if not rca or rca.startswith("# RCA Generation Failed"):
        logger.warning("Skipping ticket creation — RCA generation failed")
        return {**state, "jira_ticket": {"skipped": True, "reason": "RCA generation failed"}}

    alert_type = state["alert"].get("type", "UNKNOWN")
    summary = f"[OpsAgent] {state['incident_id']} — {alert_type} on {state['service']}"

    try:
        result = jira_client.call_tool_sync("create_ticket", {
            "incident_id": state["incident_id"],
            "service": state["service"],
            "summary": summary,
            "description": rca,
            "priority": "Critical" if alert_type in ("OOM", "POD_CRASH") else "High",
            "labels": ["opsagent", "auto-rca", alert_type.lower()],
        })
        ticket_data = result.get("result", result)
        logger.info("Jira ticket created: %s", ticket_data.get("ticket_key", "unknown"))
    except Exception as exc:
        logger.error("Jira MCP call failed: %s", exc)
        ticket_data = {"error": str(exc)}
    return {**state, "jira_ticket": ticket_data}


@observe(name="emit_report")
def emit_report(state: AgentState) -> AgentState:
    """Final node — log the completed RCA."""
    ticket_key = ""
    if state.get("jira_ticket"):
        ticket_key = state["jira_ticket"].get("ticket_key", "N/A")
    logger.info(
        "=== RCA COMPLETE === incident=%s service=%s length=%d ticket=%s",
        state["incident_id"],
        state["service"],
        len(state.get("rca_report", "")),
        ticket_key,
    )
    return state


# ---------------------------------------------------------------------------
# Build the LangGraph
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    """Construct and compile the OpsAgent state graph."""
    graph = StateGraph(AgentState)

    graph.add_node("triage", triage)
    graph.add_node("query_k8s", query_k8s)
    graph.add_node("query_metrics", query_metrics)
    graph.add_node("query_github", query_github)
    graph.add_node("synthesize_rca", synthesize_rca)
    graph.add_node("create_ticket", create_ticket)
    graph.add_node("emit_report", emit_report)

    graph.set_entry_point("triage")
    graph.add_edge("triage", "query_k8s")
    graph.add_edge("query_k8s", "query_metrics")
    graph.add_edge("query_metrics", "query_github")
    graph.add_edge("query_github", "synthesize_rca")
    graph.add_edge("synthesize_rca", "create_ticket")
    graph.add_edge("create_ticket", "emit_report")
    graph.add_edge("emit_report", END)

    return graph.compile()


# Compiled graph singleton
agent_graph = build_graph()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@observe(name="run_opsagent")
def run_agent_workflow(alert_data: dict) -> dict:
    """
    Entry point for the OpsAgent workflow.
    Accepts an alert dict and returns the final state including the RCA report.
    """
    initial_state: AgentState = {
        "alert": alert_data,
        "incident_id": "",
        "service": "",
        "k8s_data": {},
        "prom_data": {},
        "github_data": {},
        "rca_report": "",
        "jira_ticket": {},
        "error": "",
    }
    final_state = agent_graph.invoke(initial_state)
    return dict(final_state)
