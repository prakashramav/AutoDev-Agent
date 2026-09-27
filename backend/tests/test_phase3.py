"""
Phase 3 unit tests — LLM pipeline (Planner, CodeSearch, Analyzer).

All LLM calls are mocked; Docker / sandbox is also mocked so these tests
run in any CI environment with no external dependencies.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Any

# ── Fixtures & helpers ────────────────────────────────────────────────────────

SAMPLE_FILE_TREE = """
src/
  index.py
  button.py
  utils.py
tests/
  test_button.py
  test_utils.py
README.md
requirements.txt
""".strip()

SAMPLE_ISSUE = (
    "Bug: clicking the homepage button raises AttributeError in button.py "
    "because `self.handler` is accessed before being set in __init__."
)

SAMPLE_FILE_CONTENT = """\
class Button:
    def __init__(self, label: str):
        self.label = label
        # BUG: self.handler is never set here

    def click(self):
        return self.handler()   # AttributeError: 'Button' has no attr 'handler'
"""


def _make_llm_response(tool_name: str, tool_input: dict) -> MagicMock:
    """Build a mock LLMResponse for tool_use calls."""
    from llm.client import LLMUsage, LLMResponse
    resp = MagicMock(spec=LLMResponse)
    resp.text        = ""
    resp.tool_name   = tool_name
    resp.tool_input  = tool_input
    resp.stop_reason = "tool_use"
    usage = MagicMock(spec=LLMUsage)
    usage.input_tokens  = 100
    usage.output_tokens = 200
    usage.cost_usd      = 0.0015
    resp.usage = usage
    return resp


def _make_sandbox(file_content: str = SAMPLE_FILE_CONTENT) -> MagicMock:
    """Build a mock SandboxManager."""
    sandbox = MagicMock()
    sandbox.config = MagicMock()
    sandbox.config.repo_dir = "/repo"

    async def _read_file(path: str) -> str:
        return file_content

    async def _run_command(cmd: str):
        result = MagicMock()
        # Simulate grep output
        result.stdout = (
            "/repo/src/button.py:7:        return self.handler()   "
            "# AttributeError\n"
            "/repo/src/button.py:4:        # BUG: self.handler\n"
        )
        result.stderr   = ""
        result.exit_code = 0
        return result

    sandbox.read_file   = AsyncMock(side_effect=_read_file)
    sandbox.run_command = AsyncMock(side_effect=_run_command)
    return sandbox


# ─────────────────────────────────────────────────────────────────────────────
# LLM Client tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGeminiClient:
    """Unit tests for llm/client.py."""

    @pytest.mark.asyncio
    async def test_chat_returns_text(self):
        from llm.client import GeminiClient, LLMUsage

        client = GeminiClient(api_key="fake-key")

        mock_msg = MagicMock()
        mock_msg.model = "gemini-2.5-flash"
        mock_msg.stop_reason = "end_turn"
        mock_msg.usage_metadata.prompt_token_count = 50
        mock_msg.usage_metadata.candidates_token_count = 80
        mock_msg.text = "Hello from Gemini"

        with patch.object(client, "_call", new=AsyncMock(return_value=mock_msg)):
            resp = await client.chat(system="sys", user="hello")

        assert resp.text == "Hello from Gemini"
        assert resp.usage.input_tokens  == 50
        assert resp.usage.output_tokens == 80
        assert resp.tool_input is None

    @pytest.mark.asyncio
    async def test_chat_with_tool_extracts_tool_input(self):
        from llm.client import GeminiClient

        client = GeminiClient(api_key="fake-key")

        mock_msg = MagicMock()
        mock_msg.model = "gemini-2.5-flash"
        mock_msg.stop_reason = "STOP"
        mock_msg.usage_metadata.prompt_token_count = 120
        mock_msg.usage_metadata.candidates_token_count = 300
        mock_msg.parsed = {"key": "value"}
        mock_msg.text = '{"key": "value"}'

        with patch.object(client, "_call", new=AsyncMock(return_value=mock_msg)):
            resp = await client.chat_with_tool(
                system="sys",
                user="user msg",
                tool_name="my_tool",
                tool_description="test",
                tool_schema={"type": "object", "properties": {}, "required": []},
            )

        assert resp.tool_name  == "my_tool"
        assert resp.tool_input == {"key": "value"}

    def test_cost_calculation(self):
        from llm.client import LLMUsage, _PRICE_PER_1M_INPUT, _PRICE_PER_1M_OUTPUT

        usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000, model="x")
        expected = _PRICE_PER_1M_INPUT + _PRICE_PER_1M_OUTPUT
        assert abs(usage.cost_usd - expected) < 0.001

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self):
        from llm.client import GeminiClient, LLMError

        client = GeminiClient(api_key="")
        with pytest.raises(LLMError, match="GEMINI_API_KEY"):
            await client.chat(system="s", user="u")


# ─────────────────────────────────────────────────────────────────────────────
# Planner tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanner:
    """Unit tests for agents/planner.py."""

    @pytest.mark.asyncio
    async def test_plan_returns_structured_result(self):
        from agents.planner import Planner

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "plan_issue",
            {
                "summary":        "Button crashes because handler is unset.",
                "relevant_files": ["src/button.py", "tests/test_button.py"],
                "search_queries": ["self.handler", "AttributeError"],
                "plan_text":      "## Plan\nAdd `self.handler = None` in `__init__`.",
            },
        ))

        planner = Planner(client=mock_client)
        result  = await planner.plan(
            issue_text=SAMPLE_ISSUE,
            file_tree=SAMPLE_FILE_TREE,
        )

        assert result.summary         == "Button crashes because handler is unset."
        assert result.relevant_files  == ["src/button.py", "tests/test_button.py"]
        assert result.search_queries  == ["self.handler", "AttributeError"]
        assert "Plan" in result.plan_text

    @pytest.mark.asyncio
    async def test_plan_fallback_when_tool_not_called(self):
        """If the model returns text instead of tool_use, fallback gracefully."""
        from agents.planner import Planner
        from llm.client import LLMUsage, LLMResponse

        usage = MagicMock(spec=LLMUsage)
        usage.input_tokens  = 10
        usage.output_tokens = 20
        usage.cost_usd      = 0.0

        resp = MagicMock(spec=LLMResponse)
        resp.text       = "Here is my plan: fix the handler."
        resp.tool_input = None
        resp.usage      = usage

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=resp)

        planner = Planner(client=mock_client)
        result  = await planner.plan(
            issue_text=SAMPLE_ISSUE,
            file_tree=SAMPLE_FILE_TREE,
        )

        assert result.relevant_files == []
        assert result.search_queries == []
        assert "plan" in result.plan_text.lower()

    @pytest.mark.asyncio
    async def test_plan_truncates_large_tree(self):
        """Prompt builder should truncate trees > 500 lines without crashing."""
        from agents.planner import Planner

        big_tree = "\n".join(f"src/file_{i}.py" for i in range(1000))

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "plan_issue",
            {
                "summary":        "big repo",
                "relevant_files": [],
                "search_queries": [],
                "plan_text":      "plan",
            },
        ))

        planner = Planner(client=mock_client)
        result  = await planner.plan(issue_text="issue", file_tree=big_tree)
        assert result.summary == "big repo"

        # Verify the prompt sent to the client was truncated
        call_kwargs = mock_client.chat_with_tool.call_args.kwargs
        assert "omitted" in call_kwargs["user"]

    def test_validate_tool_input_missing_key(self):
        from agents.planner import Planner

        with pytest.raises(ValueError, match="missing keys"):
            Planner._validate_tool_input({"summary": "x"})  # missing 3 keys

    def test_validate_tool_input_wrong_type(self):
        from agents.planner import Planner

        with pytest.raises(ValueError, match="must be a list"):
            Planner._validate_tool_input({
                "summary": "x",
                "relevant_files": "not-a-list",
                "search_queries": [],
                "plan_text": "p",
            })


# ─────────────────────────────────────────────────────────────────────────────
# CodeSearch tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCodeSearch:
    """Unit tests for agents/code_search.py."""

    @pytest.mark.asyncio
    async def test_search_parses_grep_output(self):
        from agents.code_search import CodeSearch

        sandbox = _make_sandbox()
        searcher = CodeSearch(sandbox)
        result   = await searcher.search(["self.handler"])

        assert len(result.matches) > 0
        for m in result.matches:
            assert m.file != ""
            assert m.line > 0

    @pytest.mark.asyncio
    async def test_search_deduplicates_hits(self):
        """Same (file, line) hit from two queries should appear only once."""
        from agents.code_search import CodeSearch

        sandbox = _make_sandbox()
        # Two queries that will return the same grep output
        searcher = CodeSearch(sandbox)
        result   = await searcher.search(["self.handler", "AttributeError"])

        hits = {(m.file, m.line) for m in result.matches}
        assert len(hits) == len(result.matches), "Duplicate hits found"

    @pytest.mark.asyncio
    async def test_search_empty_queries_returns_empty(self):
        from agents.code_search import CodeSearch

        sandbox = _make_sandbox()
        searcher = CodeSearch(sandbox)
        result   = await searcher.search([])

        assert result.matches      == []
        assert result.queries_run  == []

    @pytest.mark.asyncio
    async def test_search_grep_no_match(self):
        """grep exits 1 (no match) — should not crash, just return empty list."""
        from agents.code_search import CodeSearch

        sandbox = MagicMock()
        sandbox.config = MagicMock()
        sandbox.config.repo_dir = "/repo"

        async def _no_match(cmd: str):
            result = MagicMock()
            result.stdout    = ""
            result.stderr    = ""
            result.exit_code = 1   # grep convention: 1 = no match
            return result

        sandbox.run_command = AsyncMock(side_effect=_no_match)

        searcher = CodeSearch(sandbox)
        result   = await searcher.search(["xyzzy_nonexistent"])

        assert result.matches        == []
        assert "xyzzy_nonexistent" in result.queries_empty

    def test_sanitise_query_strips_dangerous_chars(self):
        from agents.code_search import _sanitise_query

        raw     = "rm -rf / && echo 'pwned'"
        cleaned = _sanitise_query(raw)
        assert "&&"     not in cleaned
        assert "'"      not in cleaned
        assert "pwned"  in cleaned   # safe word preserved

    def test_parse_grep_output(self):
        from agents.code_search import _parse_grep_output

        stdout = "/repo/src/button.py:42:    return self.handler()\n"
        hits   = _parse_grep_output(stdout, "handler", "/repo")

        assert len(hits) == 1
        assert hits[0].file    == "src/button.py"
        assert hits[0].line    == 42
        assert hits[0].content == "return self.handler()"


# ─────────────────────────────────────────────────────────────────────────────
# Analyzer tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzer:
    """Unit tests for agents/analyzer.py."""

    @pytest.mark.asyncio
    async def test_analyze_reads_files_and_returns_plan(self):
        from agents.analyzer import Analyzer

        sandbox = _make_sandbox()

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "write_fix_plan",
            {
                "fix_plan":      "## Fix\nAdd `self.handler = None` in `__init__`.",
                "files_to_edit": ["src/button.py"],
                "tests_to_run":  ["tests/test_button.py"],
                "confidence":    "high",
            },
        ))

        analyzer = Analyzer(sandbox=sandbox, client=mock_client)
        result   = await analyzer.analyze(
            issue_text=SAMPLE_ISSUE,
            planner_plan="Add handler to __init__.",
            relevant_files=["src/button.py"],
            search_hits=[{"file": "src/button.py", "line": 7, "content": "self.handler()", "query": "handler"}],
        )

        assert "Fix" in result.fix_plan
        assert result.files_to_edit == ["src/button.py"]
        assert result.tests_to_run  == ["tests/test_button.py"]
        assert result.confidence     == "high"
        assert "src/button.py" in result.files_read

    @pytest.mark.asyncio
    async def test_analyze_handles_read_failure_gracefully(self):
        """If a file can't be read, skip it and continue."""
        from agents.analyzer import Analyzer

        sandbox = MagicMock()
        sandbox.config = MagicMock()
        sandbox.read_file = AsyncMock(side_effect=RuntimeError("file not found"))

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "write_fix_plan",
            {
                "fix_plan":      "Fallback plan",
                "files_to_edit": [],
                "tests_to_run":  [],
                "confidence":    "low",
            },
        ))

        analyzer = Analyzer(sandbox=sandbox, client=mock_client)
        result   = await analyzer.analyze(
            issue_text=SAMPLE_ISSUE,
            planner_plan="plan",
            relevant_files=["missing_file.py"],
        )

        assert result.fix_plan  == "Fallback plan"
        assert result.files_read == []

    @pytest.mark.asyncio
    async def test_analyze_text_fallback_when_no_tool(self):
        """If the model doesn't call the tool, fall back to text output."""
        from agents.analyzer import Analyzer
        from llm.client import LLMUsage, LLMResponse

        sandbox = _make_sandbox()

        usage = MagicMock(spec=LLMUsage)
        usage.input_tokens  = 10
        usage.output_tokens = 20
        usage.cost_usd      = 0.0

        resp = MagicMock(spec=LLMResponse)
        resp.text       = "Here is the fix: add handler in __init__."
        resp.tool_input = None
        resp.usage      = usage

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=resp)

        analyzer = Analyzer(sandbox=sandbox, client=mock_client)
        result   = await analyzer.analyze(
            issue_text=SAMPLE_ISSUE,
            planner_plan="plan",
            relevant_files=["src/button.py"],
        )

        assert "fix" in result.fix_plan.lower()
        assert result.confidence == "low"

    @pytest.mark.asyncio
    async def test_analyze_limits_files_read(self):
        """Should read at most MAX_FILES_TO_READ files even if more are provided."""
        from agents.analyzer import Analyzer, MAX_FILES_TO_READ

        read_calls: list[str] = []

        sandbox = MagicMock()
        sandbox.config = MagicMock()

        async def _read(path: str) -> str:
            read_calls.append(path)
            return "content"

        sandbox.read_file = AsyncMock(side_effect=_read)

        mock_client = AsyncMock()
        mock_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "write_fix_plan",
            {
                "fix_plan": "plan", "files_to_edit": [],
                "tests_to_run": [], "confidence": "medium",
            },
        ))

        many_files = [f"src/file_{i}.py" for i in range(20)]

        analyzer = Analyzer(sandbox=sandbox, client=mock_client)
        await analyzer.analyze(
            issue_text="issue",
            planner_plan="plan",
            relevant_files=many_files,
        )

        assert len(read_calls) <= MAX_FILES_TO_READ


# ─────────────────────────────────────────────────────────────────────────────
# Integration-style test: Planner → CodeSearch → Analyzer
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase3Pipeline:
    """End-to-end test of the full Phase 3 pipeline with mocked LLM + sandbox."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self):
        from agents.planner import Planner
        from agents.code_search import CodeSearch
        from agents.analyzer import Analyzer

        sandbox = _make_sandbox()

        # 1. Planner
        planner_client = AsyncMock()
        planner_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "plan_issue",
            {
                "summary":        "Handler attribute missing.",
                "relevant_files": ["src/button.py"],
                "search_queries": ["self.handler"],
                "plan_text":      "Add `self.handler = None`.",
            },
        ))
        planner        = Planner(client=planner_client)
        planner_result = await planner.plan(SAMPLE_ISSUE, SAMPLE_FILE_TREE)

        # 2. CodeSearch
        searcher      = CodeSearch(sandbox)
        search_result = await searcher.search(planner_result.search_queries)

        # 3. Analyzer
        analyzer_client = AsyncMock()
        analyzer_client.chat_with_tool = AsyncMock(return_value=_make_llm_response(
            "write_fix_plan",
            {
                "fix_plan":      "## Fix\nIn `Button.__init__` add `self.handler = None`.",
                "files_to_edit": ["src/button.py"],
                "tests_to_run":  ["tests/test_button.py"],
                "confidence":    "high",
            },
        ))
        analyzer = Analyzer(sandbox=sandbox, client=analyzer_client)
        hits_dicts = [
            {"file": m.file, "line": m.line, "content": m.content, "query": m.query}
            for m in search_result.matches
        ]
        analyzer_result = await analyzer.analyze(
            issue_text=SAMPLE_ISSUE,
            planner_plan=planner_result.plan_text,
            relevant_files=planner_result.relevant_files + search_result.unique_files,
            search_hits=hits_dicts,
        )

        # Assertions on the full pipeline output
        assert planner_result.relevant_files  == ["src/button.py"]
        assert len(search_result.matches)     > 0
        assert "Fix" in analyzer_result.fix_plan
        assert analyzer_result.confidence     == "high"
