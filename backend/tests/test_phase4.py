"""
Phase 4 unit and integration tests — Code Agent, Test Agent, and the
Modify → Test → Iterate loop.

All external LLM and Docker calls are mocked for reliable CI execution.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.coder import CodeAgent, FileEdit, CodeAgentResult
from agents.tester import TestAgent, TestAgentRunResult
from sandbox.parser import TestResult, TestStatus, TestCase


SAMPLE_FIX_PLAN = """\
## Fix Plan
In `src/calc.py`, update `add` to cast arguments to integers.
"""

SAMPLE_ISSUE = "Bug: calc.add('1', '2') concatenates strings instead of returning 3."


def _make_llm_coder_response(edits: list[dict], explanation: str = "Fix add method") -> MagicMock:
    from llm.client import LLMUsage, LLMResponse
    resp = MagicMock(spec=LLMResponse)
    resp.text = ""
    resp.tool_name = "apply_edits"
    resp.tool_input = {
        "explanation": explanation,
        "edits": edits,
    }
    resp.stop_reason = "tool_use"
    usage = MagicMock(spec=LLMUsage)
    usage.input_tokens = 250
    usage.output_tokens = 150
    usage.cost_usd = 0.003
    resp.usage = usage
    return resp


class TestCodeAgent:
    """Unit tests for agents/coder.py."""

    @pytest.mark.asyncio
    async def test_generate_and_apply_edits_writes_to_sandbox(self):
        sandbox = MagicMock()
        sandbox.config = MagicMock()
        sandbox.config.repo_dir = "/repo"
        sandbox.read_file = AsyncMock(return_value="def add(a, b):\n    return a + b\n")
        sandbox.write_file = AsyncMock()

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(
            return_value=_make_llm_coder_response(
                edits=[
                    {
                        "path": "src/calc.py",
                        "content": "def add(a, b):\n    return int(a) + int(b)\n",
                        "explanation": "Cast parameters to int",
                    }
                ]
            )
        )

        coder = CodeAgent(sandbox=sandbox, client=mock_client)
        res = await coder.generate_and_apply_edits(
            issue_text=SAMPLE_ISSUE,
            fix_plan=SAMPLE_FIX_PLAN,
            target_files=["src/calc.py"],
        )

        assert len(res.edits) == 1
        assert res.edits[0].path == "src/calc.py"
        assert "int(a)" in res.edits[0].content
        sandbox.write_file.assert_called_once_with(
            "/repo/src/calc.py",
            "def add(a, b):\n    return int(a) + int(b)\n",
        )

    @pytest.mark.asyncio
    async def test_code_agent_handles_new_file_creation(self):
        sandbox = MagicMock()
        sandbox.config = MagicMock()
        sandbox.config.repo_dir = "/repo"
        # read_file fails because file doesn't exist yet
        sandbox.read_file = AsyncMock(side_effect=FileNotFoundError("missing"))
        sandbox.write_file = AsyncMock()

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(
            return_value=_make_llm_coder_response(
                edits=[
                    {
                        "path": "tests/test_new.py",
                        "content": "def test_it(): assert True\n",
                    }
                ]
            )
        )

        coder = CodeAgent(sandbox=sandbox, client=mock_client)
        res = await coder.generate_and_apply_edits(
            issue_text="Add test",
            fix_plan="Create tests/test_new.py",
            target_files=["tests/test_new.py"],
        )

        assert len(res.edits) == 1
        assert res.edits[0].path == "tests/test_new.py"
        sandbox.write_file.assert_called_once()


class TestTestAgent:
    """Unit tests for agents/tester.py."""

    @pytest.mark.asyncio
    async def test_execute_tests_passing(self):
        sandbox = MagicMock()
        passing_result = TestResult(
            passed=5,
            failed=0,
            errors=0,
            status=TestStatus.PASSED,
            exit_code=0,
        )
        sandbox.run_tests = AsyncMock(return_value=passing_result)

        tester = TestAgent(sandbox=sandbox)
        res = await tester.execute_tests()

        assert res.passed is True
        assert "5 passed" in res.summary
        assert res.failure_feedback == ""

    @pytest.mark.asyncio
    async def test_execute_tests_failing_formats_feedback(self):
        sandbox = MagicMock()
        failing_result = TestResult(
            passed=4,
            failed=1,
            errors=0,
            status=TestStatus.FAILED,
            exit_code=1,
            failure_summary="AssertionError: 2 != 3 in test_add",
        )
        sandbox.run_tests = AsyncMock(return_value=failing_result)

        tester = TestAgent(sandbox=sandbox)
        res = await tester.execute_tests(specific_tests=["tests/test_calc.py"])

        assert res.passed is False
        assert "1 failed" in res.summary
        assert "AssertionError: 2 != 3" in res.failure_feedback
        sandbox.run_tests.assert_called_once_with(extra_args="tests/test_calc.py")


class TestPhase4IterationLoop:
    """Integration test simulating Code Agent + Test Agent iteration loop."""

    @pytest.mark.asyncio
    async def test_modify_test_iterate_healing(self):
        sandbox = MagicMock()
        sandbox.config = MagicMock()
        sandbox.config.repo_dir = "/repo"
        sandbox.read_file = AsyncMock(return_value="def add(a, b): return a + b\n")
        sandbox.write_file = AsyncMock()

        # Step 1: initial run fails
        test_fail = TestResult(
            passed=0,
            failed=1,
            errors=0,
            status=TestStatus.FAILED,
            exit_code=1,
            failure_summary="AssertionError: '12' != 3",
        )
        # Step 2: second run passes
        test_pass = TestResult(
            passed=1,
            failed=0,
            errors=0,
            status=TestStatus.PASSED,
            exit_code=0,
        )

        sandbox.run_tests = AsyncMock(side_effect=[test_fail, test_pass])

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(
            side_effect=[
                # Attempt 1: naive edit
                _make_llm_coder_response(
                    edits=[{"path": "calc.py", "content": "def add(a, b): return str(a) + str(b)"}]
                ),
                # Attempt 2: corrected edit after seeing failure traceback
                _make_llm_coder_response(
                    edits=[{"path": "calc.py", "content": "def add(a, b): return int(a) + int(b)"}]
                ),
            ]
        )

        coder = CodeAgent(sandbox=sandbox, client=mock_client)
        tester = TestAgent(sandbox=sandbox)

        # Iteration 1
        r1 = await coder.generate_and_apply_edits(
            issue_text=SAMPLE_ISSUE,
            fix_plan="Fix add",
            target_files=["calc.py"],
            iteration=1,
        )
        t1 = await tester.execute_tests()
        assert t1.passed is False

        # Iteration 2 with feedback
        r2 = await coder.generate_and_apply_edits(
            issue_text=SAMPLE_ISSUE,
            fix_plan="Fix add",
            target_files=["calc.py"],
            test_feedback=t1.failure_feedback,
            iteration=2,
        )
        t2 = await tester.execute_tests()
        assert t2.passed is True
        assert sandbox.write_file.call_count == 2
