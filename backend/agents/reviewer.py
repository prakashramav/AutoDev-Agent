"""
Review Agent — Phase 5.

Performs automated security, code quality, and regression reviews on
unified diffs generated during a run before allowing PR creation.

Checks:
  - Secrets & Credentials (API keys, hardcoded passwords, tokens, private keys)
  - Security Vulnerabilities (command injection, path traversal, unsafe deserialization, SQL injection)
  - Regression Risks (unintended deletions, breaking changes, scope creep)
  - Cleanliness & Quality (debug statements, print calls, leftover comments)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from llm.client import AnthropicClient, LLMResponse, get_llm_client

logger = structlog.get_logger(__name__)

# Common secret heuristics (regex fast-pass before LLM)
_SECRET_PATTERNS = [
    (r"(?i)(password|passwd|secret|api_key|apikey|access_token|private_key)\s*[:=]\s*['\"][A-Za-z0-9_\-\.\/+=]{8,}['\"]", "Hardcoded credential or token"),
    (r"-----BEGIN (RSA|OPENSSH|DSA|EC|PGP) PRIVATE KEY-----", "Private key header"),
    (r"(?i)ghp_[A-Za-z0-9]{36}", "GitHub Personal Access Token"),
    (r"(?i)sk-[A-Za-z0-9]{32,}", "Secret key token"),
    (r"(?i)AKIA[0-9A-Z]{16}", "AWS Access Key"),
]


@dataclass
class ReviewResult:
    """Output of the Review Agent security and quality pass."""
    approved: bool
    summary: str
    security_concerns: list[str] = field(default_factory=list)
    quality_notes: list[str] = field(default_factory=list)
    full_review_markdown: str = ""
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


_SYSTEM_PROMPT = """\
You are AutoDev's Review Agent, a senior security engineer and code reviewer.
Your job is to thoroughly analyze the unified git diff produced for a bug fix or feature.

Evaluate:
1. SECURITY:
   - Are there any leaked secrets, tokens, or credentials?
   - Is there potential for command injection, path traversal, SQL injection, or unvalidated input execution?
   - Are unsafe imports or dangerous functions introduced?
2. QUALITY & CORRECTNESS:
   - Does the diff fix the described issue without unintended side effects?
   - Is there scope creep or unnecessary modification to unrelated files?
   - Are there leftover debug statements (e.g. print, console.log) or commented-out code?
3. DECISION:
   - Approve if the changes are safe, correct, and clean.
   - Reject if any high-severity security flaw or secret leak is detected.
"""

_REVIEW_DIFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["approved", "summary", "security_concerns", "quality_notes", "full_review_markdown"],
    "properties": {
        "approved": {
            "type": "boolean",
            "description": "Whether the diff is safe and approved for pull request creation.",
        },
        "summary": {
            "type": "string",
            "description": "Short 1-2 sentence executive summary of review findings.",
        },
        "security_concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of any security flaws, secret leaks, or vulnerabilities identified.",
        },
        "quality_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Constructive suggestions, code style notes, or minor observations.",
        },
        "full_review_markdown": {
            "type": "string",
            "description": "Complete markdown review notes suitable for posting as a PR review.",
        },
    },
}


class ReviewAgent:
    """
    Review Agent evaluating code diffs for security, secrets, and correctness.
    """

    def __init__(self, client: AnthropicClient | None = None) -> None:
        self._client = client or get_llm_client()

    def run_fast_heuristic_check(self, diff: str) -> list[str]:
        """Perform instant regex-based secret checks on added lines in the diff."""
        findings = []
        added_lines = [
            line[1:].strip()
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        text_to_check = "\n".join(added_lines)

        for pattern, description in _SECRET_PATTERNS:
            if re.search(pattern, text_to_check):
                findings.append(f"Potential secret detected: {description}")

        return findings

    async def review_diff(
        self,
        issue_text: str,
        diff: str,
        test_summary: str | None = None,
    ) -> ReviewResult:
        """
        Analyze git diff against the issue description and test results.
        """
        logger.info("review_agent_start", diff_length=len(diff))

        if not diff.strip():
            return ReviewResult(
                approved=False,
                summary="Diff is empty; no changes made to review.",
                security_concerns=["No modifications found"],
                full_review_markdown="## Review Result: FAILED\n\nNo changes were made to the repository.",
            )

        # 1. Fast heuristics
        heuristic_warnings = self.run_fast_heuristic_check(diff)

        # 2. LLM Review
        user_prompt = (
            f"## Issue Description\n{issue_text}\n\n"
            f"## Test Results\n{test_summary or 'No test execution data'}\n\n"
            f"## Unified Git Diff\n```diff\n{diff[:15000]}\n```\n\n"
            "Review the diff thoroughly and call the `review_diff` tool."
        )

        resp: LLMResponse = await self._client.chat_with_tool(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tool_name="review_diff",
            tool_description="Submit structured security and quality review of the diff.",
            tool_schema=_REVIEW_DIFF_SCHEMA,
        )

        if not resp.tool_input:
            logger.warning("review_agent_no_tool_input", stop_reason=resp.stop_reason)
            approved = len(heuristic_warnings) == 0
            return ReviewResult(
                approved=approved,
                summary="Automated fallback review completed.",
                security_concerns=heuristic_warnings,
                full_review_markdown=resp.text or "Review completed with fallback parsing.",
                usage_input_tokens=resp.usage.input_tokens,
                usage_output_tokens=resp.usage.output_tokens,
            )

        data = resp.tool_input
        security_concerns = data.get("security_concerns", [])
        # Merge heuristic findings if any
        for h in heuristic_warnings:
            if h not in security_concerns:
                security_concerns.append(h)

        approved = data.get("approved", True)
        if heuristic_warnings:
            approved = False

        res = ReviewResult(
            approved=approved,
            summary=data.get("summary", ""),
            security_concerns=security_concerns,
            quality_notes=data.get("quality_notes", []),
            full_review_markdown=data.get("full_review_markdown", ""),
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
        )

        logger.info(
            "review_agent_complete",
            approved=res.approved,
            concerns=len(res.security_concerns),
        )
        return res
