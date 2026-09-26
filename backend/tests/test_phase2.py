"""
Phase 2 unit tests — sandbox primitives, parser, and config.

All tests mock docker so no real Docker daemon is required.
Run with:  cd backend && pytest tests/test_phase2.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

# ─── Parser tests ─────────────────────────────────────────────────────────────

class TestParsePytestOutput:
    """Tests for sandbox.parser.parse_pytest_output"""

    def _parse(self, stdout: str, exit_code: int = 0) -> "TestResult":
        from sandbox.parser import parse_pytest_output
        return parse_pytest_output(stdout=stdout, stderr="", exit_code=exit_code)

    def test_all_passed(self):
        output = (
            "......\n"
            "6 passed in 0.42s\n"
        )
        result = self._parse(output, exit_code=0)
        assert result.passed == 6
        assert result.failed == 0
        assert result.all_passed is True
        from sandbox.parser import TestStatus
        assert result.status == TestStatus.PASSED

    def test_some_failed(self):
        output = (
            "....F\n"
            "FAILED tests/test_auth.py::test_login_500 - AssertionError: expected 200\n"
            "4 passed, 1 failed in 0.55s\n"
        )
        result = self._parse(output, exit_code=1)
        assert result.passed == 4
        assert result.failed == 1
        assert result.all_passed is False
        from sandbox.parser import TestStatus
        assert result.status == TestStatus.FAILED
        assert len(result.cases) == 1
        assert result.cases[0].nodeid == "tests/test_auth.py::test_login_500"

    def test_collection_error(self):
        output = (
            "ERROR collecting tests/test_broken.py\n"
            "ImportError: No module named 'missing_dep'\n"
        )
        result = self._parse(output, exit_code=2)
        from sandbox.parser import TestStatus
        assert result.status == TestStatus.ERROR

    def test_no_tests_collected(self):
        output = "no tests ran\n"
        result = self._parse(output, exit_code=5)
        from sandbox.parser import TestStatus
        assert result.status == TestStatus.UNKNOWN
        assert "No tests" in result.failure_summary

    def test_mixed_skipped(self):
        output = "3 passed, 2 skipped in 0.30s\n"
        result = self._parse(output, exit_code=0)
        assert result.passed == 3
        assert result.skipped == 2
        assert result.all_passed is True

    def test_to_dict(self):
        output = "1 passed in 0.10s\n"
        result = self._parse(output, exit_code=0)
        d = result.to_dict()
        assert d["passed"] == 1
        assert d["all_passed"] is True
        assert "status" in d
        assert "failure_summary" in d

    def test_total_property(self):
        output = "2 passed, 1 failed, 1 skipped in 0.20s\n"
        result = self._parse(output, exit_code=1)
        assert result.total == 4

    def test_raw_output_preserved(self):
        output = "1 passed in 0.10s\n"
        result = self._parse(output, exit_code=0)
        assert "passed" in result.raw_output

    def test_warnings_parsed(self):
        output = "5 passed, 2 warnings in 0.50s\n"
        result = self._parse(output, exit_code=0)
        assert result.warnings == 2
        assert result.passed == 5


# ─── SandboxConfig tests ──────────────────────────────────────────────────────

class TestSandboxConfig:
    """Tests for sandbox.config.SandboxConfig"""

    def test_defaults_from_settings(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        # These should come from settings
        assert cfg.repo_dir == "/repo"
        assert cfg.pids_limit == 64
        assert cfg.no_new_privileges is True
        assert cfg.read_only is True

    def test_as_docker_run_flags_contains_required_fields(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        flags = cfg.as_docker_run_flags("test-container", "test-run-id")
        flags_str = " ".join(flags)
        assert "--detach" in flags
        assert "--read-only" in flags
        assert "--cpu-quota" in flags_str
        assert "--memory" in flags_str
        assert "--pids-limit" in flags_str
        assert "autodev=true" in flags_str
        assert "test-run-id" in flags_str

    def test_cap_drop_all_by_default(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        flags = cfg.as_docker_run_flags("c", "r")
        assert "--cap-drop" in flags
        idx = flags.index("--cap-drop")
        assert flags[idx + 1] == "ALL"

    def test_custom_memory_limit(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig(memory_limit="256m")
        flags = cfg.as_docker_run_flags("c", "r")
        assert "--memory=256m" in flags

    def test_cap_add_included_when_set(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig(cap_add=["NET_BIND_SERVICE"])
        flags = cfg.as_docker_run_flags("c", "r")
        assert "--cap-add" in flags
        idx = flags.index("--cap-add")
        assert flags[idx + 1] == "NET_BIND_SERVICE"

    def test_no_new_privileges_flag(self):
        from sandbox.config import SandboxConfig
        cfg = SandboxConfig(no_new_privileges=True)
        flags = cfg.as_docker_run_flags("c", "r")
        assert "--security-opt" in flags
        idx = flags.index("--security-opt")
        assert flags[idx + 1] == "no-new-privileges"


# ─── SandboxManager unit tests (mocked docker) ───────────────────────────────

class FakeCR:
    """Fake CommandResult for mock returns."""
    def __init__(self, stdout="", stderr="", exit_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    @property
    def success(self):
        return self.exit_code == 0

    @property
    def combined(self):
        return (self.stdout + " " + self.stderr).strip()


@pytest.fixture
def sandbox():
    """SandboxManager with a pre-injected container_id (no real Docker)."""
    from sandbox.manager import SandboxManager
    from sandbox.config import SandboxConfig
    cfg = SandboxConfig(
        image="python:3.12-slim",
        cpu_quota=50_000,
        memory_limit="512m",
        command_timeout=60,
        network="sandbox_net",
    )
    sb = SandboxManager(
        repo_url="https://github.com/testowner/testrepo",
        run_id="test-run-id-5678",
        config=cfg,
    )
    sb.container_id = "fakeid_abc123def456"
    return sb


@pytest.mark.asyncio
async def test_list_files_calls_find(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout="/repo/README.md\n/repo/app.py\n")
    ) as mock_exec:
        result = await sandbox.list_files()
    args = mock_exec.call_args[0][0]
    assert "find" in args
    # Results should be sorted
    assert "/repo/README.md" in result
    assert "/repo/app.py" in result


@pytest.mark.asyncio
async def test_read_file_success(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout="# Hello\n")
    ):
        content = await sandbox.read_file("/repo/README.md")
    assert content == "# Hello\n"


@pytest.mark.asyncio
async def test_read_file_raises_not_found(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout="", stderr="No such file", exit_code=1)
    ):
        with pytest.raises(FileNotFoundError):
            await sandbox.read_file("/repo/missing.py")


@pytest.mark.asyncio
async def test_write_file_uses_base64(sandbox):
    """write_file should use base64 encoding — no /tmp on host."""
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR()
    ) as mock_exec:
        await sandbox.write_file("/repo/new_file.py", "print('hello')")

    # The command should contain base64 decode
    call_args = mock_exec.call_args[0][0]
    cmd_str = " ".join(call_args)
    assert "base64" in cmd_str or "base64" in str(mock_exec.call_args)


@pytest.mark.asyncio
async def test_write_file_fails_raises_runtime_error(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stderr="Permission denied", exit_code=1)
    ):
        with pytest.raises(RuntimeError, match="write_file failed"):
            await sandbox.write_file("/readonly/path.py", "content")


@pytest.mark.asyncio
async def test_file_exists_true(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(exit_code=0)
    ):
        assert await sandbox.file_exists("/repo/requirements.txt") is True


@pytest.mark.asyncio
async def test_file_exists_false(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(exit_code=1)
    ):
        assert await sandbox.file_exists("/repo/nonexistent.txt") is False


@pytest.mark.asyncio
async def test_run_tests_returns_test_result(sandbox):
    """run_tests() should return a TestResult, not a CommandResult."""
    from sandbox.parser import TestResult
    pytest_output = "3 passed in 0.30s\n"
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout=pytest_output, exit_code=0)
    ):
        result = await sandbox.run_tests()
    assert isinstance(result, TestResult)
    assert result.passed == 3
    assert result.all_passed is True


@pytest.mark.asyncio
async def test_run_tests_failure_has_summary(sandbox):
    """run_tests() should parse failures and set failure_summary."""
    from sandbox.parser import TestStatus
    pytest_output = (
        "FAILED tests/test_api.py::test_login - AssertionError\n"
        "1 failed in 0.10s\n"
    )
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout=pytest_output, exit_code=1)
    ):
        result = await sandbox.run_tests()
    assert result.status == TestStatus.FAILED
    assert result.failed == 1


@pytest.mark.asyncio
async def test_install_dependencies_pip(sandbox):
    """install_dependencies() should detect requirements.txt and run pip."""
    calls = []

    async def fake_exec(cmd, **kwargs):
        calls.append(cmd)
        # First call is file_exists check for requirements.txt
        if cmd[0] == "test":
            return FakeCR(exit_code=0)  # file exists
        return FakeCR(stdout="Successfully installed\n")

    with patch.object(sandbox, "_exec", side_effect=fake_exec):
        result = await sandbox.install_dependencies()

    # Should have found requirements.txt and run pip
    pip_calls = [c for c in calls if "pip" in " ".join(c)]
    assert len(pip_calls) >= 1


@pytest.mark.asyncio
async def test_install_dependencies_no_dep_file(sandbox):
    """install_dependencies() should return 0-exit result when no dep file found."""
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(exit_code=1)  # all `test -f` checks fail
    ):
        result = await sandbox.install_dependencies()
    assert result.exit_code == 0
    assert "No recognized" in result.stdout


@pytest.mark.asyncio
async def test_run_command_wraps_in_sh(sandbox):
    with patch.object(
        sandbox, "_exec", new_callable=AsyncMock,
        return_value=FakeCR(stdout="output\n")
    ) as mock_exec:
        result = await sandbox.run_command("echo hello")
    assert result.stdout == "output\n"
    # _exec receives the cmd list that includes sh -c
    args = mock_exec.call_args[0][0]
    assert "sh" in args and "-c" in args


@pytest.mark.asyncio
async def test_destroy_calls_docker_rm(sandbox):
    with patch.object(
        sandbox, "_host_run", new_callable=AsyncMock,
        return_value=FakeCR()
    ) as mock_host:
        await sandbox.destroy()
    call_args = mock_host.call_args[0][0]
    assert "docker" in call_args
    assert "rm" in call_args
    assert "-f" in call_args
    assert sandbox.container_id is None  # cleared after destroy


@pytest.mark.asyncio
async def test_destroy_no_op_without_container():
    from sandbox.manager import SandboxManager
    sb = SandboxManager(repo_url="https://github.com/x/y")
    # container_id is None — should not raise
    await sb.destroy()


@pytest.mark.asyncio
async def test_exec_raises_without_container():
    from sandbox.manager import SandboxManager
    sb = SandboxManager(repo_url="https://github.com/x/y")
    with pytest.raises(RuntimeError, match="not started"):
        await sb._exec(["ls"])


@pytest.mark.asyncio
async def test_host_run_timeout_returns_exit_124():
    from sandbox.manager import SandboxManager
    import asyncio

    async def hanging_communicate(input=None):
        await asyncio.sleep(9999)
        return b"", b""

    mock_proc = AsyncMock()
    mock_proc.communicate = hanging_communicate
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await SandboxManager._host_run(["sleep", "infinity"], timeout=0.01)

    assert result.exit_code == 124
    assert "timed out" in result.stderr.lower()


def test_command_result_success_and_combined():
    from sandbox.manager import CommandResult
    ok = CommandResult(stdout="hello\n", stderr="", exit_code=0)
    fail = CommandResult(stdout="", stderr="error\n", exit_code=1)
    assert ok.success is True
    assert fail.success is False
    assert "hello" in ok.combined
    assert "error" in fail.combined


def test_repr():
    from sandbox.manager import SandboxManager
    sb = SandboxManager(repo_url="https://github.com/x/y", run_id="abc-123")
    assert "not_started" in repr(sb)
    sb.container_id = "deadbeefcafe1234"
    assert "deadbeefcafe" in repr(sb)
