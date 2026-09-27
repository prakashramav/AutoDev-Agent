"""
GitHub Client & Integration Service — Phase 6.

Handles interactions with GitHub:
  - Parsing repository owner and name from URLs
  - Fetching remote issue details
  - Creating git branches, committing code in the sandbox, and pushing
  - Opening Pull Requests via GitHub REST API
  - Dev-mode simulation / mock PR generation when tokens are absent
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from core.config import settings

logger = structlog.get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class RepoIdentifier:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class PRCreationResult:
    pr_url: str
    pr_number: int | None
    branch_name: str
    title: str
    body: str
    is_mock: bool = False


def parse_github_repo_url(url: str) -> RepoIdentifier:
    """
    Extract owner and repo name from GitHub URL or owner/repo format.
    Supports:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
      - owner/repo
    """
    clean_url = url.strip().rstrip("/")
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]

    # Match https://github.com/owner/repo or http
    https_match = re.search(r"github\.com[/:]([^/]+)/([^/]+)", clean_url)
    if https_match:
        return RepoIdentifier(owner=https_match.group(1), repo=https_match.group(2))

    # Match owner/repo
    parts = clean_url.split("/")
    if len(parts) == 2:
        return RepoIdentifier(owner=parts[0], repo=parts[1])

    raise ValueError(f"Could not parse valid GitHub owner and repository from '{url}'")


class GitHubService:
    """
    GitHub API client for issue inspection and pull request creation.
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else settings.GITHUB_TOKEN
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AutoDev-Agent",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    async def get_issue(self, repo_url: str, issue_number: int) -> dict[str, Any] | None:
        """Fetch title and body of an issue from GitHub."""
        try:
            repo_id = parse_github_repo_url(repo_url)
            url = f"{GITHUB_API_BASE}/repos/{repo_id.full_name}/issues/{issue_number}"
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.get(url, headers=self.headers)
                if res.status_code == 200:
                    data = res.json()
                    return {
                        "title": data.get("title", ""),
                        "body": data.get("body", ""),
                        "number": issue_number,
                    }
                logger.warning("github_issue_fetch_failed", status=res.status_code, repo=repo_id.full_name)
        except Exception as exc:
            logger.warning("github_issue_fetch_error", exc=str(exc))
        return None

    async def create_pull_request(
        self,
        repo_url: str,
        branch_name: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> PRCreationResult:
        """
        Open a Pull Request on GitHub. If no GITHUB_TOKEN is configured or
        running in offline development mode, returns a synthetic simulated PR.
        """
        repo_id = parse_github_repo_url(repo_url)

        if not self.token:
            logger.info("no_github_token_generating_simulated_pr", repo=repo_id.full_name)
            mock_url = f"https://github.com/{repo_id.full_name}/pull/simulate-{branch_name}"
            return PRCreationResult(
                pr_url=mock_url,
                pr_number=999,
                branch_name=branch_name,
                title=title,
                body=body,
                is_mock=True,
            )

        url = f"{GITHUB_API_BASE}/repos/{repo_id.full_name}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": branch_name,
            "base": base_branch,
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(url, headers=self.headers, json=payload)
                if res.status_code in (200, 201):
                    data = res.json()
                    pr_url = data.get("html_url", "")
                    pr_num = data.get("number")
                    logger.info("github_pr_created", pr_url=pr_url, number=pr_num)
                    return PRCreationResult(
                        pr_url=pr_url,
                        pr_number=pr_num,
                        branch_name=branch_name,
                        title=title,
                        body=body,
                        is_mock=False,
                    )
                else:
                    err_msg = res.text
                    logger.error("github_pr_creation_failed", status=res.status_code, response=err_msg)
                    raise RuntimeError(f"GitHub PR creation failed ({res.status_code}): {err_msg}")
        except Exception as exc:
            logger.exception("github_pr_creation_error")
            raise
