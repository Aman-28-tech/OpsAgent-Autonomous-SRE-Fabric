"""
OpsAgent — RabbitMQ Alert Queue Consumer
==========================================
Pulls alert messages from the RabbitMQ queue and runs the LangGraph
agent workflow for each.  This decouples alert ingestion (HTTP webhook)
from agent processing, enabling:
  • At-least-once delivery (messages are ACK'd only after RCA completes)
  • Horizontal scaling (run N consumers in parallel)
  • Back-pressure handling (RabbitMQ buffers bursts of alerts)

Usage
-----
    python queue_consumer.py          # blocks, pulls from RABBITMQ_URL
"""

import json
import logging
import os
import sys
import time

import pika

import config

logger = logging.getLogger("opsagent.consumer")
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [CONSUMER] %(levelname)s %(name)s — %(message)s",
)

QUEUE_NAME = os.getenv("ALERT_QUEUE_NAME", "opsagent.alerts")


def _connect(url: str) -> pika.BlockingConnection:
    """Connect to RabbitMQ with retry."""
    for attempt in range(1, 11):
        try:
            params = pika.URLParameters(url)
            conn = pika.BlockingConnection(params)
            logger.info("Connected to RabbitMQ on attempt %d", attempt)
            return conn
        except pika.exceptions.AMQPConnectionError:
            wait = min(2 ** attempt, 30)
            logger.warning("RabbitMQ not ready — retry in %ds (attempt %d/10)", wait, attempt)
            time.sleep(wait)
    logger.error("Failed to connect to RabbitMQ after 10 attempts")
    sys.exit(1)


def _on_message(ch, method, properties, body):
    """Callback for each alert message."""
    try:
        alert_data = json.loads(body)
        logger.info("Received alert: type=%s service=%s", alert_data.get("type"), alert_data.get("service"))

        # Import here to avoid circular dependency and allow lazy LLM init
        from workflow import run_agent_workflow

        start = time.perf_counter()
        result = run_agent_workflow(alert_data)
        elapsed = time.perf_counter() - start

        incident_id = result.get("incident_id", "UNKNOWN")
        logger.info("RCA complete: incident=%s in %.1fs", incident_id, elapsed)

        # ACK only on success — message will be requeued on failure
        ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as exc:
        logger.exception("Workflow failed for message: %s", exc)
        # NACK and requeue for retry
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def main():
    """Start consuming alerts from the queue."""
    rabbitmq_url = config.RABBITMQ_URL
    logger.info("Starting consumer — queue=%s url=%s", QUEUE_NAME, rabbitmq_url.split("@")[-1])

    connection = _connect(rabbitmq_url)
    channel = connection.channel()

    # Declare the queue (idempotent)
    channel.queue_declare(queue=QUEUE_NAME, durable=True)

    # Fair dispatch — don't give a consumer more than 1 unacked message
    channel.basic_qos(prefetch_count=1)

    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=_on_message)

    logger.info("Waiting for alerts on queue '%s'. Press Ctrl+C to stop.", QUEUE_NAME)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        logger.info("Consumer shutting down")
        channel.stop_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
