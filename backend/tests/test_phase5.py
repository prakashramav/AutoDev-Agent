"""
Phase 5 unit and integration tests — Multi-Agent Supervisor (LangGraph)
and Review Agent (Security, Secrets, & Code Quality).

Mocked LLM and sandbox execution to ensure high-speed, deterministic tests.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.reviewer import ReviewAgent, ReviewResult
from agents.supervisor import (
    create_supervisor_graph,
    should_continue_testing,
    AgentState,
)
from sandbox.parser import TestResult, TestStatus
from models.run import RunStatus


SAMPLE_SAFE_DIFF = """\
--- a/src/math.py
+++ b/src/math.py
@@ -1,3 +1,3 @@
 def add(a, b):
-    return a - b
+    return a + b
"""

SAMPLE_LEAKY_DIFF = """\
--- a/src/config.py
+++ b/src/config.py
@@ -1,2 +1,3 @@
-API_KEY = os.getenv("API_KEY")
+API_KEY = "ghp_1234567890abcdef1234567890abcdef1234"
+password = "SuperSecretP@ssword123"
"""


def _make_llm_reviewer_response(approved: bool, concerns: list[str], summary: str = "Review passed") -> MagicMock:
    from llm.client import LLMUsage, LLMResponse
    resp = MagicMock(spec=LLMResponse)
    resp.text = ""
    resp.tool_name = "review_diff"
    resp.tool_input = {
        "approved": approved,
        "summary": summary,
        "security_concerns": concerns,
        "quality_notes": ["Good change"],
        "full_review_markdown": f"## Review\n\nApproved: {approved}\n\n{summary}",
    }
    resp.stop_reason = "tool_use"
    usage = MagicMock(spec=LLMUsage)
    usage.input_tokens = 300
    usage.output_tokens = 100
    usage.cost_usd = 0.002
    resp.usage = usage
    return resp


class TestReviewAgent:
    """Unit tests for agents/reviewer.py."""

    def test_heuristic_detects_credentials(self):
        reviewer = ReviewAgent()
        findings = reviewer.run_fast_heuristic_check(SAMPLE_LEAKY_DIFF)
        assert len(findings) >= 2
        assert any("GitHub Personal Access Token" in f for f in findings)
        assert any("Hardcoded credential" in f for f in findings)

    def test_heuristic_passes_clean_diff(self):
        reviewer = ReviewAgent()
        findings = reviewer.run_fast_heuristic_check(SAMPLE_SAFE_DIFF)
        assert len(findings) == 0

    @pytest.mark.asyncio
    async def test_review_approved_diff(self):
        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(
            return_value=_make_llm_reviewer_response(approved=True, concerns=[])
        )

        reviewer = ReviewAgent(client=mock_client)
        result = await reviewer.review_diff(
            issue_text="Fix addition bug in math.py",
            diff=SAMPLE_SAFE_DIFF,
            test_summary="1 passed in 0.01s",
        )

        assert result.approved is True
        assert len(result.security_concerns) == 0
        assert "Review" in result.full_review_markdown

    @pytest.mark.asyncio
    async def test_review_rejects_leaky_diff_even_if_llm_approves(self):
        # Even if mock LLM said approved, static heuristic flag overrides it to False
        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(
            return_value=_make_llm_reviewer_response(approved=True, concerns=[])
        )

        reviewer = ReviewAgent(client=mock_client)
        result = await reviewer.review_diff(
            issue_text="Update config",
            diff=SAMPLE_LEAKY_DIFF,
        )

        assert result.approved is False
        assert len(result.security_concerns) > 0


class TestSupervisorGraphRouting:
    """Unit tests for LangGraph conditional edges and routing logic."""

    def test_routing_to_review_when_tests_pass(self):
        state: AgentState = {
            "all_tests_passed": True,
            "iteration": 1,
            "max_iterations": 3,
        }  # type: ignore
        next_step = should_continue_testing(state)
        assert next_step == "review"

    def test_routing_to_modify_when_tests_fail_under_limit(self):
        state: AgentState = {
            "all_tests_passed": False,
            "iteration": 1,
            "max_iterations": 3,
        }  # type: ignore
        next_step = should_continue_testing(state)
        assert next_step == "modify"

    def test_routing_to_review_when_max_iterations_exhausted(self):
        state: AgentState = {
            "all_tests_passed": False,
            "iteration": 3,
            "max_iterations": 3,
        }  # type: ignore
        next_step = should_continue_testing(state)
        assert next_step == "review"

    def test_graph_structure_compilation(self):
        """Ensure the StateGraph compiles cleanly with all required nodes."""
        graph = create_supervisor_graph()
        assert graph is not None
        # Check node presence in the compiled graph
        nodes = graph.nodes
        for required_node in ["inspect", "plan", "analyze", "modify", "test", "review"]:
            assert required_node in nodes
