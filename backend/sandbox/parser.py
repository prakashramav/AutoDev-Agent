"""
Pytest output parser — converts raw pytest stdout into a structured TestResult.

Designed to be a pure, side-effect-free module so it can be unit-tested
without any Docker or network dependencies.

Parsed from pytest's default `--tb=short -q` output format.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TestStatus(str, Enum):
    PASSED  = "passed"
    FAILED  = "failed"
    ERROR   = "error"    # collection / import error — no tests ran
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass
class TestCase:
    """A single pytest test case result."""
    nodeid: str                      # e.g. tests/test_foo.py::test_bar
    status: TestStatus
    duration_seconds: Optional[float] = None
    short_traceback: str = ""        # captured from --tb=short


@dataclass
class TestResult:
    """
    Structured result of a full pytest run.

    Produced by :func:`parse_pytest_output` from raw stdout/stderr.
    All counts come from the pytest summary line; individual cases are
    extracted on a best-effort basis.
    """
    # ── Raw ────────────────────────────────────────────────────────
    raw_output: str = ""
    exit_code: int = 0

    # ── Summary counts ─────────────────────────────────────────────
    passed:  int = 0
    failed:  int = 0
    errors:  int = 0
    skipped: int = 0
    warnings: int = 0

    # ── Individual test cases (best-effort parse) ──────────────────
    cases: list[TestCase] = field(default_factory=list)

    # ── High-level status ──────────────────────────────────────────
    status: TestStatus = TestStatus.UNKNOWN

    # ── Failure summary (for feeding back to Code Agent) ───────────
    failure_summary: str = ""   # concatenated tracebacks for failed cases

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def all_passed(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "total": self.total,
            "all_passed": self.all_passed,
            "failure_summary": self.failure_summary,
            "cases": [
                {
                    "nodeid": c.nodeid,
                    "status": c.status.value,
                    "duration_seconds": c.duration_seconds,
                    "short_traceback": c.short_traceback,
                }
                for c in self.cases
            ],
        }


# ── Regex patterns ─────────────────────────────────────────────────────────

# e.g.  "5 passed, 2 failed, 1 error, 3 skipped, 4 warnings in 0.45s"
_SUMMARY_RE = re.compile(
    r"(?:(\d+)\s+passed)?"
    r"(?:,?\s*(\d+)\s+failed)?"
    r"(?:,?\s*(\d+)\s+error(?:s)?)?"
    r"(?:,?\s*(\d+)\s+skipped)?"
    r"(?:,?\s*(\d+)\s+warning(?:s)?)?"
    r"\s+in\s+([\d.]+)s",
    re.IGNORECASE,
)

# Individual PASSED/FAILED lines from -v output or the short summary
# e.g.  "FAILED tests/test_auth.py::test_login_500 - AssertionError: ..."
_FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<nodeid>[^\s]+)\s*-?\s*(?P<reason>.*)?$", re.MULTILINE
)

# no-tests-collected
_NO_TESTS_RE = re.compile(r"no tests ran|no tests collected", re.IGNORECASE)

# collection / import error
_COLLECTION_ERROR_RE = re.compile(
    r"ERROR collecting|ImportError|ModuleNotFoundError", re.IGNORECASE
)


def parse_pytest_output(stdout: str, stderr: str, exit_code: int) -> TestResult:
    """
    Parse raw pytest output (stdout + stderr) into a :class:`TestResult`.

    This is intentionally lenient — pytest output format varies by version
    and plugin, so we degrade gracefully when patterns don't match.

    Args:
        stdout: Combined stdout from the pytest process.
        stderr: Combined stderr from the pytest process.
        exit_code: Process exit code (0 = all passed, 1 = some failed,
                   2 = interrupted, 3 = internal error, 4 = usage error,
                   5 = no tests collected).

    Returns:
        A populated :class:`TestResult`.
    """
    combined = stdout + "\n" + stderr
    result = TestResult(raw_output=combined, exit_code=exit_code)

    # ── No tests collected ─────────────────────────────────────────
    if exit_code == 5 or _NO_TESTS_RE.search(combined):
        result.status = TestStatus.UNKNOWN
        result.failure_summary = "No tests were collected."
        return result

    # ── Collection / import error ──────────────────────────────────
    if _COLLECTION_ERROR_RE.search(combined):
        result.status = TestStatus.ERROR
        result.failure_summary = _extract_error_block(combined)
        return result

    # ── Parse summary line ─────────────────────────────────────────
    m = _SUMMARY_RE.search(combined)
    if m:
        result.passed  = int(m.group(1) or 0)
        result.failed  = int(m.group(2) or 0)
        result.errors  = int(m.group(3) or 0)
        result.skipped = int(m.group(4) or 0)
        result.warnings = int(m.group(5) or 0)

    # ── Parse individual FAILED lines ─────────────────────────────
    failed_cases: list[TestCase] = []
    for fm in _FAILED_LINE_RE.finditer(combined):
        failed_cases.append(
            TestCase(
                nodeid=fm.group("nodeid").strip(),
                status=TestStatus.FAILED,
                short_traceback=fm.group("reason").strip() if fm.group("reason") else "",
            )
        )
    result.cases = failed_cases

    # ── Build failure summary for Code Agent feedback ─────────────
    if result.failed or result.errors:
        result.failure_summary = _extract_failure_blocks(combined)

    # ── Determine top-level status ─────────────────────────────────
    if exit_code == 0:
        result.status = TestStatus.PASSED
    elif result.errors:
        result.status = TestStatus.ERROR
    elif result.failed:
        result.status = TestStatus.FAILED
    else:
        result.status = TestStatus.UNKNOWN

    return result


# ── Private helpers ────────────────────────────────────────────────────────

def _extract_failure_blocks(text: str) -> str:
    """
    Extract the short failure/traceback blocks from pytest output.
    Returns a condensed string suitable for LLM feedback.
    """
    lines = text.splitlines()
    blocks: list[str] = []
    current_block: list[str] = []
    in_block = False

    for line in lines:
        # pytest separator for short tracebacks
        if re.match(r"^_{5,}|^-{5,}", line):
            if current_block:
                blocks.append("\n".join(current_block))
                current_block = []
            in_block = True
        elif in_block:
            current_block.append(line)

    if current_block:
        blocks.append("\n".join(current_block))

    if blocks:
        return "\n\n".join(blocks[:10])  # cap at 10 blocks to avoid token bloat

    # Fallback: return last 60 lines
    return "\n".join(lines[-60:])


def _extract_error_block(text: str) -> str:
    """Extract collection/import error context."""
    lines = text.splitlines()
    error_lines = [
        l for l in lines
        if any(kw in l for kw in ("Error", "error", "Traceback", "File "))
    ]
    return "\n".join(error_lines[:30])
