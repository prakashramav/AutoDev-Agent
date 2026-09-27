"""
Planner agent — Phase 3.

Given an issue description and the repository's file tree, the Planner:
  1. Understands what the issue is asking for.
  2. Produces a ranked list of files most likely to be relevant.
  3. Generates code-search queries (grep patterns) to find exact locations.
  4. Produces a high-level natural-language plan of what to change.

The Planner uses structured output (tool_use) so callers get a typed dict
rather than free-form text that needs additional parsing.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Any

from llm.client import GeminiClient, LLMError, LLMResponse, get_llm_client

logger = structlog.get_logger(__name__)


# ─── Output types ─────────────────────────────────────────────────────────────

@dataclass
class PlannerResult:
    """
    Structured output of the Planner agent.

    Attributes
    ----------
    summary:
        1–3 sentence description of what the issue asks for.
    relevant_files:
        Ranked list of file paths (most likely to need changes first).
        The Analyzer will read these files in order.
    search_queries:
        Grep-style patterns (plain strings, not regexes) to search inside
        the repo.  Used by CodeSearch in the next step.
    plan_text:
        High-level natural-language plan describing what changes to make.
        Written before any code is read, so it may be refined later.
    usage_input_tokens:
        Total input tokens consumed (for cost tracking).
    usage_output_tokens:
        Total output tokens consumed.
    """
    summary:              str
    relevant_files:       list[str]
    search_queries:       list[str]
    plan_text:            str
    usage_input_tokens:   int = 0
    usage_output_tokens:  int = 0


# ── Prompt templates ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are AutoDev, an expert AI software engineer.
Your job is to analyse a GitHub issue and a repository file tree, then produce
a structured plan for resolving the issue.

Rules:
- Be concise and precise.
- Rank files by likelihood of needing a change (most likely first).
- Search queries should be short, literal strings (not regexes) that will
  appear in the relevant code.
- The plan should describe WHAT to change, not HOW to write every line of code.
  Leave implementation details for a later step when you have read the files.
- If the issue is a bug: identify the faulty component first.
- If the issue is a feature: identify where to add the entry point first.
"""


def _build_user_prompt(
    issue_text: str,
    file_tree:  str,
    repo_meta:  dict[str, Any] | None = None,
) -> str:
    meta_block = ""
    if repo_meta:
        meta_block = f"""
## Repository metadata
- Primary language : {repo_meta.get('primary_language', 'unknown')}
- Test framework   : {repo_meta.get('test_framework', 'unknown')}
- Dependency files : {', '.join(repo_meta.get('dep_files', [])) or 'none'}
- Test directories : {', '.join(repo_meta.get('test_dirs', [])) or 'none'}
"""
    return f"""\
## Issue
{issue_text}
{meta_block}
## Repository file tree (truncated to 500 lines)
```
{_truncate_tree(file_tree, max_lines=500)}
```

Analyse the issue and produce a structured plan using the `plan_issue` tool.
"""


def _truncate_tree(tree: str, max_lines: int) -> str:
    lines = tree.splitlines()
    if len(lines) <= max_lines:
        return tree
    half = max_lines // 2
    return "\n".join(lines[:half]) + f"\n... [{len(lines) - max_lines} lines omitted] ...\n" + "\n".join(lines[-half:])


# ── Tool schema ───────────────────────────────────────────────────────────────

_PLAN_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "relevant_files", "search_queries", "plan_text"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "1–3 sentence description of what the issue is asking for.",
        },
        "relevant_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ranked list of repository file paths most likely to need changes. "
                "Use paths exactly as they appear in the file tree. "
                "List at most 15 files."
            ),
        },
        "search_queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Short literal strings to grep for inside the repository. "
                "These help locate the exact lines that need changing. "
                "At most 10 queries."
            ),
        },
        "plan_text": {
            "type": "string",
            "description": (
                "High-level plan: what to change and why. "
                "Use markdown. Do not write actual code yet."
            ),
        },
    },
}


# ─── Planner ──────────────────────────────────────────────────────────────────

class Planner:
    """
    Phase-3 planning agent.

    Example::

        planner = Planner()
        result = await planner.plan(
            issue_text="Button on homepage crashes app when clicked",
            file_tree="src/\\n  index.js\\n  button.js\\n...",
        )
        print(result.relevant_files)  # ['src/button.js', 'src/index.js']
    """

    def __init__(self, client: GeminiClient | None = None) -> None:
        self._client = client or get_llm_client()

    async def plan(
        self,
        issue_text: str,
        file_tree:  str,
        repo_meta:  dict[str, Any] | None = None,
    ) -> PlannerResult:
        """
        Analyse the issue and file tree; return a structured plan.

        Raises
        ------
        LLMError
            If the LLM call fails or returns an unusable response.
        ValueError
            If the tool response is missing required fields.
        """
        logger.info("planner_start", issue_len=len(issue_text), tree_lines=file_tree.count("\n"))

        user_prompt = _build_user_prompt(issue_text, file_tree, repo_meta)

        resp: LLMResponse = await self._client.chat_with_tool(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tool_name="plan_issue",
            tool_description="Produce a structured plan for resolving the GitHub issue.",
            tool_schema=_PLAN_ISSUE_SCHEMA,
        )

        if resp.tool_input is None:
            # Fallback: try to extract from text
            logger.warning("planner_tool_not_called_falling_back_to_text")
            return self._parse_text_fallback(resp)

        tool_in = resp.tool_input
        self._validate_tool_input(tool_in)

        result = PlannerResult(
            summary=tool_in["summary"],
            relevant_files=tool_in["relevant_files"],
            search_queries=tool_in["search_queries"],
            plan_text=tool_in["plan_text"],
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
        )

        logger.info(
            "planner_done",
            files=len(result.relevant_files),
            queries=len(result.search_queries),
            cost_usd=round(resp.usage.cost_usd, 5),
        )
        return result

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_tool_input(data: dict[str, Any]) -> None:
        required = {"summary", "relevant_files", "search_queries", "plan_text"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Planner tool response missing keys: {missing}")
        if not isinstance(data["relevant_files"], list):
            raise ValueError("relevant_files must be a list")
        if not isinstance(data["search_queries"], list):
            raise ValueError("search_queries must be a list")

    @staticmethod
    def _parse_text_fallback(resp: LLMResponse) -> PlannerResult:
        """Best-effort parse when the model didn't call the tool."""
        return PlannerResult(
            summary="(fallback) " + resp.text[:300],
            relevant_files=[],
            search_queries=[],
            plan_text=resp.text,
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
        )
