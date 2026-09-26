"""
Test Agent — Phase 4.

The Test Agent executes tests inside the sandbox, evaluates failures,
identifies whether new regression tests are needed, and formats actionable
failure feedback for the Code Agent iteration loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import structlog

from sandbox.parser import TestResult, TestStatus

if TYPE_CHECKING:
    from sandbox.manager import SandboxManager

logger = structlog.get_logger(__name__)


@dataclass
class TestAgentRunResult:
    """Outcome of a test suite run processed by Test Agent."""
    passed: bool
    summary: str
    failure_feedback: str = ""
    test_result: TestResult | None = None


class TestAgent:
    """
    Test Agent that drives test execution in the sandbox and formats
    tracebacks and error contexts for self-healing loops.
    """

    def __init__(self, sandbox: "SandboxManager") -> None:
        self._sandbox = sandbox

    async def execute_tests(
        self,
        specific_tests: list[str] | None = None,
    ) -> TestAgentRunResult:
        """
        Run test suite in the sandbox container and interpret results.
        """
        extra_args = ""
        if specific_tests:
            # Join multiple test files/targets
            extra_args = " ".join(specific_tests)

        logger.info("test_agent_running_tests", specific_tests=specific_tests)

        test_result: TestResult = await self._sandbox.run_tests(extra_args=extra_args)

        passed = test_result.all_passed
        summary = (
            f"Tests: {test_result.passed} passed, {test_result.failed} failed, "
            f"{test_result.errors} errors (status: {test_result.status.value})"
        )

        failure_feedback = ""
        if not passed:
            feedback_parts = [summary]
            if test_result.failure_summary:
                feedback_parts.append("\nDetailed Failure Traces:")
                feedback_parts.append(test_result.failure_summary)
            elif test_result.raw_output:
                # Include tail of output if failure summary regex missed something
                feedback_parts.append("\nRaw Test Output:")
                feedback_parts.append("\n".join(test_result.raw_output.splitlines()[-40:]))

            failure_feedback = "\n".join(feedback_parts)

        logger.info(
            "test_agent_complete",
            passed=passed,
            summary=summary,
        )

        return TestAgentRunResult(
            passed=passed,
            summary=summary,
            failure_feedback=failure_feedback,
            test_result=test_result,
        )
