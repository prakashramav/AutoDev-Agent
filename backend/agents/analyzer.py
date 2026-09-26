"""
Analyzer agent — Phase 3.

The Analyzer reads the top-ranked relevant files (from the Planner + CodeSearch)
and, combining them with the issue description and the Planner's high-level plan,
produces a **concrete, actionable fix plan**.

This is the last LLM step before code editing (Phase 4).  The output is written
to `Run.fix_plan` in the database.

Design decisions:
  - We read at most MAX_FILES_TO_READ files, in rank order.
  - Each file is truncated to MAX_FILE_CHARS to avoid blowing the context window.
  - The prompt includes grep search hits so the model can jump straight to the
    relevant lines without reading entire files.
  - Structured output (tool_use) so the task_runner can store fields separately.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from llm.client import AnthropicClient, LLMError, LLMResponse, get_llm_client

if TYPE_CHECKING:
    from sandbox.manager import SandboxManager

logger = structlog.get_logger(__name__)

# Context budget controls
MAX_FILES_TO_READ = 8        # read at most N files
MAX_FILE_CHARS    = 8_000    # truncate each file to N characters (~2k tokens)
MAX_SEARCH_HITS   = 30       # include at most N grep hits in the prompt


# ─── Output type ──────────────────────────────────────────────────────────────

@dataclass
class AnalyzerResult:
    """
    Concrete fix plan produced by the Analyzer.

    Attributes
    ----------
    fix_plan:
        Markdown description of the exact changes to make.  Should reference
        specific file names, function names, and line numbers.
    files_to_edit:
        Specific files the coder should modify (subset of relevant_files).
    tests_to_run:
        Test files or patterns to run to verify the fix.
    confidence:
        "high" / "medium" / "low" — the model's self-assessed confidence.
    usage_input_tokens:
        Total input tokens consumed.
    usage_output_tokens:
        Total output tokens consumed.
    files_read:
        Files that were successfully read from the sandbox.
    """
    fix_plan:             str
    files_to_edit:        list[str]
    tests_to_run:         list[str]
    confidence:           str
    usage_input_tokens:   int = 0
    usage_output_tokens:  int = 0
    files_read:           list[str] = field(default_factory=list)


# ── Prompt templates ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are AutoDev, an expert AI software engineer performing a code review.

You have been given:
1. A GitHub issue description.
2. A high-level plan produced by a planning agent.
3. The content of the most relevant source files.
4. Grep search hits showing where key terms appear in the codebase.

Your task is to produce a CONCRETE fix plan — referencing specific files,
functions, classes, and line numbers.  Be precise.  Do not suggest vague
refactors; tell the coder exactly what to change.

Rules:
- Reference file paths and function/class names explicitly.
- Note line numbers where you know them (from grep hits or file content).
- If you need to add a new function, say where to add it and what signature.
- If you need to change existing code, quote the before/after clearly.
- Keep the plan under 1500 words.
- Be honest about confidence level.
"""


def _build_user_prompt(
    issue_text:    str,
    planner_plan:  str,
    file_contents: dict[str, str],
    search_hits:   list[dict[str, Any]],
) -> str:
    parts: list[str] = [
        f"## Issue\n{issue_text}\n",
        f"## High-level plan (from Planner)\n{planner_plan}\n",
    ]

    if search_hits:
        hits_block = "\n".join(
            f"  {h['file']}:{h['line']}  {h['content']}"
            for h in search_hits[:MAX_SEARCH_HITS]
        )
        parts.append(f"## Grep search hits\n```\n{hits_block}\n```\n")

    if file_contents:
        parts.append("## File contents (truncated)")
        for path, content in file_contents.items():
            parts.append(f"\n### {path}\n```\n{content}\n```")

    parts.append(
        "\nUsing all the above information, call the `write_fix_plan` tool "
        "to produce a concrete fix plan."
    )
    return "\n".join(parts)


# ── Tool schema ───────────────────────────────────────────────────────────────

_WRITE_FIX_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["fix_plan", "files_to_edit", "tests_to_run", "confidence"],
    "properties": {
        "fix_plan": {
            "type": "string",
            "description": (
                "Detailed markdown fix plan referencing specific files, functions, "
                "and line numbers.  Under 1500 words."
            ),
        },
        "files_to_edit": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact repository-relative paths of files to edit.",
        },
        "tests_to_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Test files or pytest/jest patterns to run to verify the fix. "
                "E.g. ['tests/test_button.py', 'tests/test_integration.py']."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Self-assessed confidence in the fix plan.",
        },
    },
}


# ─── Analyzer ─────────────────────────────────────────────────────────────────

class Analyzer:
    """
    Phase-3 deep-analysis agent.

    Usage::

        analyzer = Analyzer(sandbox=sandbox_manager)
        result = await analyzer.analyze(
            issue_text="...",
            planner_plan="...",
            relevant_files=["src/button.js"],
            search_hits=[{"file": "src/button.js", "line": 42, "content": "onClick"}],
        )
        print(result.fix_plan)
    """

    def __init__(
        self,
        sandbox: "SandboxManager",
        client:  AnthropicClient | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._client  = client or get_llm_client()

    async def analyze(
        self,
        issue_text:     str,
        planner_plan:   str,
        relevant_files: list[str],
        search_hits:    list[dict[str, Any]] | None = None,
    ) -> AnalyzerResult:
        """
        Read the top relevant files from the sandbox and produce a concrete plan.

        Parameters
        ----------
        issue_text:
            The raw GitHub issue text.
        planner_plan:
            The `plan_text` field from `PlannerResult`.
        relevant_files:
            Ranked file paths (from Planner) — we read the top MAX_FILES_TO_READ.
        search_hits:
            SearchMatch dicts serialised as plain dicts for prompt inclusion.
        """
        logger.info(
            "analyzer_start",
            files_available=len(relevant_files),
            has_search_hits=bool(search_hits),
        )

        # ── Read files ────────────────────────────────────────────────────────
        file_contents: dict[str, str] = {}
        files_read: list[str] = []

        for path in relevant_files[:MAX_FILES_TO_READ]:
            try:
                content = await self._sandbox.read_file(path)
                if content:
                    file_contents[path] = content[:MAX_FILE_CHARS]
                    files_read.append(path)
                    logger.info("analyzer_file_read", path=path, chars=len(content))
            except Exception as exc:
                logger.warning("analyzer_file_read_failed", path=path, exc=str(exc))

        # ── Build prompt & call LLM ───────────────────────────────────────────
        user_prompt = _build_user_prompt(
            issue_text=issue_text,
            planner_plan=planner_plan,
            file_contents=file_contents,
            search_hits=search_hits or [],
        )

        resp: LLMResponse = await self._client.chat_with_tool(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tool_name="write_fix_plan",
            tool_description="Write a concrete fix plan for the issue.",
            tool_schema=_WRITE_FIX_PLAN_SCHEMA,
        )

        if resp.tool_input is None:
            logger.warning("analyzer_tool_not_called_using_text_fallback")
            return AnalyzerResult(
                fix_plan=resp.text or "(no plan generated)",
                files_to_edit=files_read,
                tests_to_run=[],
                confidence="low",
                usage_input_tokens=resp.usage.input_tokens,
                usage_output_tokens=resp.usage.output_tokens,
                files_read=files_read,
            )

        tool_in = resp.tool_input
        result = AnalyzerResult(
            fix_plan=tool_in.get("fix_plan", ""),
            files_to_edit=tool_in.get("files_to_edit", []),
            tests_to_run=tool_in.get("tests_to_run", []),
            confidence=tool_in.get("confidence", "medium"),
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
            files_read=files_read,
        )

        logger.info(
            "analyzer_done",
            files_to_edit=len(result.files_to_edit),
            confidence=result.confidence,
            cost_usd=round(resp.usage.cost_usd, 5),
        )
        return result
