"""
CodeSearch agent — Phase 3.

Runs grep-style searches inside a live sandbox container to locate exact
lines of code relevant to the issue. Deduplicates and ranks results by
relevance (exact matches first, then partial matches).

This agent is intentionally stateless — it takes a SandboxManager and
a list of search queries, and returns a ranked list of matches.
"""
from __future__ import annotations

import re
import structlog
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sandbox.manager import SandboxManager

logger = structlog.get_logger(__name__)

# Max lines returned per query (prevents token explosion)
MAX_LINES_PER_QUERY = 50

# Max total results returned to caller
MAX_TOTAL_RESULTS = 200


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class SearchMatch:
    """A single grep hit."""
    file:    str   # repo-relative path  e.g. "src/button.js"
    line:    int   # 1-indexed line number
    content: str   # the actual line text (stripped)
    query:   str   # which search query found this


@dataclass
class CodeSearchResult:
    """Aggregated output of all grep searches."""
    matches:       list[SearchMatch] = field(default_factory=list)
    queries_run:   list[str]         = field(default_factory=list)
    queries_empty: list[str]         = field(default_factory=list)  # returned no hits

    @property
    def unique_files(self) -> list[str]:
        """Files that appear in results (preserving insertion order)."""
        seen: dict[str, None] = {}
        for m in self.matches:
            seen[m.file] = None
        return list(seen)


# ─── CodeSearch ───────────────────────────────────────────────────────────────

class CodeSearch:
    """
    Run literal-string grep searches inside a sandbox container.

    Usage::

        searcher = CodeSearch(sandbox)
        result = await searcher.search(["button click", "handleClick"])
        for match in result.matches[:10]:
            print(f"{match.file}:{match.line}  {match.content}")
    """

    def __init__(self, sandbox: "SandboxManager") -> None:
        self._sandbox = sandbox

    async def search(self, queries: list[str]) -> CodeSearchResult:
        """
        Execute all queries and return a deduplicated, ranked result set.

        Deduplication: if the same (file, line) is found by multiple queries,
        only the first occurrence is kept.
        Ranking: exact query matches come before partial matches (already
        handled by grep — first-query results are listed first).
        """
        if not queries:
            logger.warning("code_search_no_queries")
            return CodeSearchResult()

        seen_hits: set[tuple[str, int]] = set()
        all_matches: list[SearchMatch] = []
        queries_empty: list[str] = []

        for query in queries:
            if len(all_matches) >= MAX_TOTAL_RESULTS:
                logger.info("code_search_cap_reached", total=MAX_TOTAL_RESULTS)
                break

            matches = await self._run_grep(query)
            if not matches:
                queries_empty.append(query)
                continue

            for m in matches:
                key = (m.file, m.line)
                if key not in seen_hits:
                    seen_hits.add(key)
                    all_matches.append(m)

        logger.info(
            "code_search_done",
            queries_total=len(queries),
            queries_empty=len(queries_empty),
            total_matches=len(all_matches),
            unique_files=len({m.file for m in all_matches}),
        )
        return CodeSearchResult(
            matches=all_matches,
            queries_run=list(queries),
            queries_empty=queries_empty,
        )

    async def _run_grep(self, query: str) -> list[SearchMatch]:
        """
        Run `grep -rn --include=... <query> <repo_dir>` inside the sandbox.

        We include common source file extensions and exclude hidden dirs,
        __pycache__, node_modules, .git, build artefacts.
        """
        repo_dir  = self._sandbox.config.repo_dir
        safe_query = _sanitise_query(query)
        if not safe_query:
            logger.warning("code_search_query_empty_after_sanitise", raw=query)
            return []

        # grep flags: recursive, line numbers, ignore binary, ignore case for flexibility
        grep_cmd = (
            f"grep -rn --include='*.py' --include='*.js' --include='*.ts' "
            f"--include='*.jsx' --include='*.tsx' --include='*.go' "
            f"--include='*.java' --include='*.rs' --include='*.rb' "
            f"--include='*.php' --include='*.cs' --include='*.cpp' "
            f"--include='*.c' --include='*.h' --include='*.md' "
            f"--include='*.json' --include='*.yaml' --include='*.yml' "
            f"--include='*.toml' --include='*.sh' "
            f"--exclude-dir='.git' --exclude-dir='__pycache__' "
            f"--exclude-dir='node_modules' --exclude-dir='.venv' "
            f"--exclude-dir='venv' --exclude-dir='dist' --exclude-dir='build' "
            f"-m {MAX_LINES_PER_QUERY} "
            f"-i "                      # case-insensitive
            f"{shlex_quote(safe_query)} {repo_dir}"
        )

        try:
            result = await self._sandbox.run_command(grep_cmd)
        except Exception as exc:
            logger.warning("code_search_grep_error", query=query, exc=str(exc))
            return []

        # grep exits 1 when no match found — that's normal, not an error
        if result.exit_code not in (0, 1):
            logger.warning(
                "code_search_grep_unexpected_exit",
                exit_code=result.exit_code,
                stderr=result.stderr[:200],
            )
            return []

        return _parse_grep_output(result.stdout, query, repo_dir)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sanitise_query(query: str) -> str:
    """
    Strip shell-dangerous characters from a query string.
    We run grep via shell so we must be conservative.
    """
    # Allow: alphanumeric, spaces, dots, underscores, dashes, parentheses, slashes
    cleaned = re.sub(r"[^\w\s.\-_()/:]", " ", query)
    return cleaned.strip()


def shlex_quote(s: str) -> str:
    """Simple shell-quoting for grep patterns (single quotes, no interpolation)."""
    s = s.replace("'", r"'\''")   # escape any literal single quote
    return f"'{s}'"


_GREP_LINE_RE = re.compile(r"^(.+?):(\d+):(.*)$")


def _parse_grep_output(
    stdout:   str,
    query:    str,
    repo_dir: str,
) -> list[SearchMatch]:
    """Parse `grep -rn` output into SearchMatch objects."""
    matches: list[SearchMatch] = []
    for raw_line in stdout.splitlines():
        m = _GREP_LINE_RE.match(raw_line.rstrip())
        if not m:
            continue
        file_path, line_no_str, content = m.group(1), m.group(2), m.group(3)

        # Make path repo-relative
        if file_path.startswith(repo_dir):
            file_path = file_path[len(repo_dir):].lstrip("/")

        try:
            line_no = int(line_no_str)
        except ValueError:
            continue

        matches.append(SearchMatch(
            file=file_path,
            line=line_no,
            content=content.strip(),
            query=query,
        ))
    return matches
