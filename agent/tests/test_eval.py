"""
Tests — Evaluation Pipeline
=============================
Unit tests for the scoring functions in the evaluation module.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "evaluation"))

from eval import score_faithfulness, score_answer_relevancy, score_context_precision, _simulate_rca


# Sample incident data for testing
SAMPLE_INCIDENT = {
    "id": "INC-TEST",
    "type": "OOM",
    "service": "checkout-service",
    "description": "Memory exceeded limit",
    "expected_symptoms": "Pod in CrashLoopBackOff with OOMKilled exit code 137.",
    "expected_root_cause": "PR #247 introduced unbounded heap allocation in SessionCache.loadAll().",
    "expected_commit_sha": "a1b2c3d4e5f6",
    "expected_remediation": "Revert PR #247 and implement LRU eviction.",
}


class TestScoreFaithfulness:
    def test_perfect_faithfulness(self):
        """RCA with correct SHA should score high."""
        rca = "The root cause was commit a1b2c3d4e5f6 in checkout-service."
        score = score_faithfulness(rca, SAMPLE_INCIDENT)
        assert score >= 0.7

    def test_missing_sha_penalty(self):
        """RCA missing the expected SHA should be penalised."""
        rca = "The root cause was some unknown change."
        score = score_faithfulness(rca, SAMPLE_INCIDENT)
        assert score < 1.0  # Should lose at least 0.3

    def test_score_bounded(self):
        """Score should always be between 0 and 1."""
        for rca in ["", "hello world", "a" * 10000]:
            score = score_faithfulness(rca, SAMPLE_INCIDENT)
            assert 0.0 <= score <= 1.0


class TestScoreAnswerRelevancy:
    def test_relevant_rca(self):
        """RCA with all sections and keywords should score high."""
        rca = _simulate_rca(SAMPLE_INCIDENT)
        score = score_answer_relevancy(rca, SAMPLE_INCIDENT)
        assert score >= 0.8

    def test_irrelevant_rca(self):
        """Completely irrelevant text should score low."""
        rca = "The weather today is sunny with a chance of rain."
        score = score_answer_relevancy(rca, SAMPLE_INCIDENT)
        assert score < 0.3

    def test_partial_sections(self):
        """RCA with only some sections should get partial credit."""
        rca = "## Symptoms\nPod crashed.\n## Remediation\nFix it."
        score = score_answer_relevancy(rca, SAMPLE_INCIDENT)
        assert 0.2 <= score <= 0.8


class TestScoreContextPrecision:
    def test_perfect_precision(self):
        """RCA citing correct service and SHA scores 1.0."""
        rca = "checkout-service failed due to commit a1b2c3d4e5f6."
        score = score_context_precision(rca, SAMPLE_INCIDENT)
        assert score == 1.0

    def test_only_service(self):
        """RCA mentioning only the service scores 0.5."""
        rca = "checkout-service is experiencing issues."
        score = score_context_precision(rca, SAMPLE_INCIDENT)
        assert score == 0.5

    def test_only_sha(self):
        """RCA mentioning only the SHA scores 0.5."""
        rca = "The change in commit a1b2c3d4e5f6 caused issues."
        score = score_context_precision(rca, SAMPLE_INCIDENT)
        assert score == 0.5

    def test_neither(self):
        """RCA mentioning neither scores 0.0."""
        rca = "Something went wrong somewhere."
        score = score_context_precision(rca, SAMPLE_INCIDENT)
        assert score == 0.0


class TestSimulateRCA:
    def test_simulate_contains_expected_data(self):
        rca = _simulate_rca(SAMPLE_INCIDENT)
        assert SAMPLE_INCIDENT["id"] in rca
        assert SAMPLE_INCIDENT["service"] in rca
        assert SAMPLE_INCIDENT["expected_commit_sha"] in rca
        assert "Symptoms" in rca
        assert "Timeline" in rca
        assert "Root Cause" in rca
        assert "Remediation" in rca
