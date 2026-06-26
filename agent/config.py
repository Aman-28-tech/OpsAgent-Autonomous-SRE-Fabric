"""
OpsAgent — Configuration
========================
Centralised configuration loaded from environment variables with sensible
defaults for local development.
"""

import os


# -- MCP Server URLs --------------------------------------------------------
K8S_MCP_URL = os.getenv("K8S_MCP_URL", "http://k8s-mcp:8000")
PROM_MCP_URL = os.getenv("PROM_MCP_URL", "http://prom-mcp:8000")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp:8000")
JIRA_MCP_URL = os.getenv("JIRA_MCP_URL", "http://jira-mcp:8000")

# -- Auth service ------------------------------------------------------------
AUTH_URL = os.getenv("AUTH_URL", "http://auth:8000")
JWT_SECRET = os.getenv("JWT_SECRET", "opsagent-dev-secret-change-in-prod")

# -- LLM (Groq — free, fast inference) --------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

# -- Langfuse ----------------------------------------------------------------
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

# -- RabbitMQ ----------------------------------------------------------------
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
ALERT_QUEUE_NAME = os.getenv("ALERT_QUEUE_NAME", "opsagent.alerts")
USE_QUEUE = os.getenv("USE_QUEUE", "true").lower() == "true"

# -- Application -------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
RCA_QUALITY_THRESHOLD = float(os.getenv("RCA_QUALITY_THRESHOLD", "0.90"))
