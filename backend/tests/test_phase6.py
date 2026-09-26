"""
Phase 6 unit and integration tests — GitHub service, branch commit/push,
PR opening, and supervisor end-to-end PR creation.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.github import GitHubService, parse_github_repo_url, RepoIdentifier, PRCreationResult
from agents.supervisor import create_supervisor_graph, should_create_pr, AgentState
from models.run import RunStatus


class TestGitHubURLParsing:
    """Tests for parse_github_repo_url."""

    def test_https_url(self):
        res = parse_github_repo_url("https://github.com/prakashramav/AutoDev-Agent")
        assert res.owner == "prakashramav"
        assert res.repo == "AutoDev-Agent"
        assert res.full_name == "prakashramav/AutoDev-Agent"

    def test_https_url_with_git(self):
        res = parse_github_repo_url("https://github.com/octocat/Hello-World.git")
        assert res.owner == "octocat"
        assert res.repo == "Hello-World"

    def test_ssh_url(self):
        res = parse_github_repo_url("git@github.com:facebook/react.git")
        assert res.owner == "facebook"
        assert res.repo == "react"

    def test_short_owner_repo(self):
        res = parse_github_repo_url("pallets/flask")
        assert res.owner == "pallets"
        assert res.repo == "flask"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            parse_github_repo_url("invalid-url-string")


class TestGitHubService:
    """Tests for GitHubService API operations and mock mode."""

    @pytest.mark.asyncio
    async def test_simulated_pr_when_no_token(self):
        # Without token, GitHubService should return a simulated PR result without crashing
        gh = GitHubService(token="")
        res = await gh.create_pull_request(
            repo_url="https://github.com/owner/repo",
            branch_name="autodev/fix-12345678",
            title="fix: resolve login crash",
            body="Autonomous PR",
        )
        assert res.is_mock is True
        assert "simulate-autodev/fix-12345678" in res.pr_url
        assert res.pr_number == 999

    @pytest.mark.asyncio
    async def test_real_pr_calls_github_api(self):
        gh = GitHubService(token="ghp_test_token_1234567890")

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "html_url": "https://github.com/owner/repo/pull/42",
            "number": 42,
        }

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
            res = await gh.create_pull_request(
                repo_url="https://github.com/owner/repo",
                branch_name="autodev/fix-12345678",
                title="fix: test",
                body="details",
            )
            assert res.is_mock is False
            assert res.pr_url == "https://github.com/owner/repo/pull/42"
            assert res.pr_number == 42
            mock_post.assert_called_once()


class TestSupervisorPRRouting:
    """Tests for should_create_pr routing and graph integration."""

    def test_should_create_pr_when_approved_and_diff_present(self):
        state: AgentState = {
            "review_approved": True,
            "diff": "--- a/file.py\n+++ b/file.py\n+hello",
        }  # type: ignore
        assert should_create_pr(state) == "create_pr"

    def test_should_not_create_pr_when_review_rejected(self):
        state: AgentState = {
            "review_approved": False,
            "diff": "--- a/file.py\n+++ b/file.py\n+hello",
        }  # type: ignore
        assert should_create_pr(state) == "end"

    def test_should_not_create_pr_when_diff_is_empty(self):
        state: AgentState = {
            "review_approved": True,
            "diff": "",
        }  # type: ignore
        assert should_create_pr(state) == "end"

    def test_graph_has_create_pr_node(self):
        graph = create_supervisor_graph()
        assert "create_pr" in graph.nodes
