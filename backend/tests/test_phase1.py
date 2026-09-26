"""
Phase 1 tests — no Docker, no DB required.

Tests focus on:
1. API schema validation (TaskRequest)
2. SandboxManager logic (mocked docker subprocess)
3. Run model field defaults

Run with:  pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Schema validation tests ────────────────────────────────────

def test_task_request_requires_issue():
    """Must provide either issue_number or issue_text."""
    from api.routes.tasks import TaskRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskRequest(repo_url="https://github.com/owner/repo")


def test_task_request_issue_number_ok():
    from api.routes.tasks import TaskRequest
    t = TaskRequest(repo_url="https://github.com/owner/repo", issue_number=42)
    assert t.issue_number == 42


def test_task_request_issue_text_ok():
    from api.routes.tasks import TaskRequest
    t = TaskRequest(
        repo_url="https://github.com/owner/repo",
        issue_text="Login API returns 500",
    )
    assert t.issue_text == "Login API returns 500"


# ─── Run model tests ────────────────────────────────────────────

def test_run_model_defaults():
    from models.run import Run, RunStatus
    run = Run(repo_url="https://github.com/owner/repo")
    assert run.status == RunStatus.PENDING
    assert run.trace is None or run.trace == []


# ─── SandboxManager unit tests (mocked docker) ──────────────────

class FakeCommandResult:
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    @property
    def success(self):
        return self.exit_code == 0


@pytest.fixture
def sandbox():
    from sandbox.manager import SandboxManager
    sb = SandboxManager(
        repo_url="https://github.com/testowner/testrepo",
        run_id="test-run-id-1234",
    )
    # Inject a fake container id so we don't need docker
    sb.container_id = "fake_container_abc123"
    return sb


@pytest.mark.asyncio
async def test_list_files_calls_find(sandbox):
    """list_files should exec `find` inside the container."""
    with patch.object(
        sandbox,
        "_exec_in_sandbox",
        new_callable=AsyncMock,
        return_value=FakeCommandResult(stdout="/repo/README.md\n/repo/main.py\n"),
    ) as mock_exec:
        result = await sandbox.list_files()

    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][0]
    assert "find" in args
    assert result == "/repo/README.md\n/repo/main.py\n"


@pytest.mark.asyncio
async def test_read_file_returns_content(sandbox):
    """read_file should exec `cat` and return stdout."""
    with patch.object(
        sandbox,
        "_exec_in_sandbox",
        new_callable=AsyncMock,
        return_value=FakeCommandResult(stdout="# Hello World\n"),
    ):
        content = await sandbox.read_file("/repo/README.md")

    assert content == "# Hello World\n"


@pytest.mark.asyncio
async def test_read_file_raises_on_failure(sandbox):
    """read_file should raise FileNotFoundError when cat fails."""
    with patch.object(
        sandbox,
        "_exec_in_sandbox",
        new_callable=AsyncMock,
        return_value=FakeCommandResult(stdout="", stderr="No such file", exit_code=1),
    ):
        with pytest.raises(FileNotFoundError):
            await sandbox.read_file("/repo/nonexistent.py")


@pytest.mark.asyncio
async def test_run_command_passes_through(sandbox):
    """run_command should wrap cmd in sh -c and pass to _exec_in_sandbox."""
    with patch.object(
        sandbox,
        "_exec_in_sandbox",
        new_callable=AsyncMock,
        return_value=FakeCommandResult(stdout="output", stderr="", exit_code=0),
    ) as mock_exec:
        result = await sandbox.run_command("echo hello")

    assert result.success
    args = mock_exec.call_args[0][0]
    assert "sh" in args
    assert "-c" in args
    assert "echo hello" in args


@pytest.mark.asyncio
async def test_destroy_calls_docker_rm(sandbox):
    """destroy() should call docker rm -f on the container."""
    with patch.object(
        sandbox,
        "_host_run",
        new_callable=AsyncMock,
        return_value=FakeCommandResult(),
    ) as mock_host:
        await sandbox.destroy()

    call_args = mock_host.call_args[0][0]
    assert "docker" in call_args
    assert "rm" in call_args
    assert sandbox.container_id is None  # cleared after destroy


@pytest.mark.asyncio
async def test_destroy_skips_if_no_container():
    """destroy() should be a no-op if container was never started."""
    from sandbox.manager import SandboxManager
    sb = SandboxManager(repo_url="https://github.com/x/y", run_id="no-container")
    # container_id is None by default
    await sb.destroy()  # should not raise


# ─── CommandResult helper ───────────────────────────────────────

def test_command_result_success_property():
    from sandbox.manager import CommandResult
    ok = CommandResult(stdout="ok", stderr="", exit_code=0)
    fail = CommandResult(stdout="", stderr="err", exit_code=1)
    assert ok.success is True
    assert fail.success is False
