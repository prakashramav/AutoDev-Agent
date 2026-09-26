"""
Code Agent — Phase 4.

The Code Agent takes:
  1. The issue description
  2. The fix plan from the Analyzer
  3. The current contents of files to edit
  4. (Optional) Previous test failure output / compiler error for iterations

It produces concrete edits (new content for target files, or unified diffs)
and applies them to the sandbox repository via `SandboxManager.write_file()`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import structlog

from llm.client import AnthropicClient, LLMResponse, get_llm_client

if TYPE_CHECKING:
    from sandbox.manager import SandboxManager

logger = structlog.get_logger(__name__)

# Maximum characters allowed per file in prompt
MAX_FILE_CHARS_CODE_AGENT = 12_000


@dataclass
class FileEdit:
    """A file to be modified or created."""
    path: str
    content: str
    explanation: str = ""


@dataclass
class CodeAgentResult:
    """Output of the Code Agent."""
    edits: list[FileEdit] = field(default_factory=list)
    explanation: str = ""
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


_SYSTEM_PROMPT = """\
You are AutoDev's Code Agent, an expert autonomous programmer.
Your job is to apply code modifications to resolve a GitHub issue based on
an analyzer fix plan or previous test failure details.

Rules:
1. Provide the COMPLETE file content for each file being edited or created.
   Do not output truncated code, placeholders, or snippets like `// ... rest of file unchanged`.
2. Keep edits focused: ONLY modify files strictly necessary to fix the issue.
3. Preserve existing code style, formatting, imports, and indentation.
4. Ensure code syntax is 100% valid.
5. If fixing a bug after test failure, carefully review the traceback and make the precise fix.
"""

_APPLY_EDITS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["explanation", "edits"],
    "properties": {
        "explanation": {
            "type": "string",
            "description": "Brief explanation of the changes made and why.",
        },
        "edits": {
            "type": "array",
            "description": "List of files to update or create.",
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path (e.g., 'src/auth.py').",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete new content of the file.",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Short explanation of specific changes in this file.",
                    },
                },
            },
        },
    },
}


def _build_coder_prompt(
    issue_text: str,
    fix_plan: str,
    files_content: dict[str, str],
    test_feedback: str | None = None,
    iteration: int = 1,
) -> str:
    sections = [
        f"## Issue Description\n{issue_text}\n",
        f"## Fix Plan\n{fix_plan}\n",
    ]

    if test_feedback:
        sections.append(
            f"## Previous Test / Execution Failure (Iteration {iteration})\n"
            f"The previous attempt failed tests. Fix the issues below:\n```\n{test_feedback}\n```\n"
        )

    sections.append("## Current File Contents\n")
    for path, content in files_content.items():
        truncated = content[:MAX_FILE_CHARS_CODE_AGENT]
        sections.append(f"### {path}\n```\n{truncated}\n```\n")

    sections.append(
        "Call the `apply_edits` tool with complete files to resolve the issue."
    )
    return "\n".join(sections)


class CodeAgent:
    """
    Code Agent responsible for writing and modifying code in the sandbox.
    """

    def __init__(
        self,
        sandbox: "SandboxManager",
        client: AnthropicClient | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._client = client or get_llm_client()

    async def generate_and_apply_edits(
        self,
        issue_text: str,
        fix_plan: str,
        target_files: list[str],
        test_feedback: str | None = None,
        iteration: int = 1,
    ) -> CodeAgentResult:
        """
        Generate file edits using LLM and apply them to the sandbox container.
        """
        logger.info(
            "code_agent_start",
            target_files_count=len(target_files),
            iteration=iteration,
            has_feedback=bool(test_feedback),
        )

        # 1. Read target files from sandbox
        files_content: dict[str, str] = {}
        repo_dir = self._sandbox.config.repo_dir

        for rel_path in target_files:
            clean_path = rel_path.lstrip("/")
            full_path = f"{repo_dir}/{clean_path}" if not rel_path.startswith(repo_dir) else rel_path
            try:
                content = await self._sandbox.read_file(full_path)
                files_content[clean_path] = content
            except Exception as exc:
                logger.warning("read_target_file_failed", path=clean_path, exc=str(exc))
                # New file creation: provide empty string
                files_content[clean_path] = ""

        # 2. Build prompt and query LLM
        prompt = _build_coder_prompt(
            issue_text=issue_text,
            fix_plan=fix_plan,
            files_content=files_content,
            test_feedback=test_feedback,
            iteration=iteration,
        )

        resp: LLMResponse = await self._client.chat_with_tool(
            system=_SYSTEM_PROMPT,
            user=prompt,
            tool_name="apply_edits",
            tool_description="Provide complete file contents for all modified/created files.",
            tool_schema=_APPLY_EDITS_SCHEMA,
        )

        if not resp.tool_input:
            logger.warning("code_agent_no_tool_input", stop_reason=resp.stop_reason)
            return CodeAgentResult(
                explanation=resp.text or "Model did not return structured edits",
                usage_input_tokens=resp.usage.input_tokens,
                usage_output_tokens=resp.usage.output_tokens,
            )

        data = resp.tool_input
        edits_data = data.get("edits", [])
        edits: list[FileEdit] = []

        for item in edits_data:
            path = item.get("path", "").strip()
            content = item.get("content", "")
            if not path or not content:
                continue

            clean_path = path.lstrip("/")
            # Normalize to sandbox repo directory
            target_path = (
                f"{repo_dir}/{clean_path}"
                if not path.startswith(repo_dir)
                else path
            )

            # Apply edit to sandbox container
            try:
                await self._sandbox.write_file(target_path, content)
                logger.info("file_written_to_sandbox", path=clean_path, length=len(content))
                edits.append(
                    FileEdit(
                        path=clean_path,
                        content=content,
                        explanation=item.get("explanation", ""),
                    )
                )
            except Exception as exc:
                logger.error("file_write_failed", path=clean_path, exc=str(exc))

        return CodeAgentResult(
            edits=edits,
            explanation=data.get("explanation", ""),
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
        )
