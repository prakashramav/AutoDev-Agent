"""
Sandbox integration tests — Phase 2.

These tests spin up REAL Docker containers and clone a small public repo.
They validate the full sandbox primitive loop:
  clone → list_files → read_file → write_file → install_deps → run_tests

Requirements:
  - Docker daemon running and accessible
  - Internet access to github.com and pypi.org
  - The sandbox_net Docker network created (or set SANDBOX_NETWORK=bridge)

Skip by default unless the AUTODEV_INTEGRATION env var is set:

    AUTODEV_INTEGRATION=1 pytest tests/test_sandbox_integration.py -v -s

Target repo: https://github.com/psf/requests-mock
  - Small, pure-Python, well-maintained
  - Has a proper pytest test suite
  - No compiled deps (pure pip)
"""
from __future__ import annotations

import os
import pytest

# Skip unless running integration tests explicitly
pytestmark = pytest.mark.skipif(
    not os.environ.get("AUTODEV_INTEGRATION"),
    reason="Set AUTODEV_INTEGRATION=1 to run sandbox integration tests",
)

# Use a small, stable, well-maintained repo with a simple pytest suite
INTEGRATION_REPO = "https://github.com/stub42/pytz"   # very small, pure-Python
TIMEOUT_SECONDS = 300


@pytest.fixture(scope="module")
def event_loop_policy():
    """Use the default event loop policy for integration tests."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="module")
async def sandbox():
    """
    Spin up a real sandbox and clone the integration test repo.
    Shared across all tests in the module (scope=module) to avoid
    re-cloning on every test.
    """
    from sandbox.manager import SandboxManager
    from sandbox.config import SandboxConfig

    cfg = SandboxConfig(
        network=os.environ.get("SANDBOX_NETWORK", "bridge"),  # use bridge if sandbox_net not set up
        command_timeout=TIMEOUT_SECONDS,
        clone_timeout=120,
        setup_timeout=180,
        memory_limit="768m",
    )
    sb = SandboxManager(repo_url=INTEGRATION_REPO, config=cfg)
    await sb.start()
    await sb.clone_repo()
    yield sb
    await sb.destroy()


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_container_starts(sandbox):
    """Sandbox container should be running after start()."""
    assert sandbox.container_id is not None
    assert len(sandbox.container_id) > 8

    # Confirm container is live by running a trivial command
    result = await sandbox.run_command("echo ALIVE")
    assert result.success, f"Container not alive: {result.stderr}"
    assert "ALIVE" in result.stdout


@pytest.mark.asyncio
async def test_repo_cloned(sandbox):
    """Repo should be cloned into /repo with files present."""
    files = await sandbox.list_files()
    assert len(files.splitlines()) > 5, "Expected more than 5 files in repo"
    # pytz should have a setup.py or pyproject.toml
    assert any(
        "setup" in f or "pyproject" in f
        for f in files.splitlines()
    ), "No setup file found in cloned repo"


@pytest.mark.asyncio
async def test_read_file(sandbox):
    """read_file() should return file content from inside the sandbox."""
    files = await sandbox.list_files()
    # Find any .py file
    py_files = [f for f in files.splitlines() if f.endswith(".py")]
    assert py_files, "No .py files found in repo"

    content = await sandbox.read_file(py_files[0])
    assert len(content) > 0, f"Empty content for {py_files[0]}"
    # Python files should have some recognizable content
    assert any(kw in content for kw in ("import", "def", "class", "#")), \
        f"Unexpected content in {py_files[0]}: {content[:100]}"


@pytest.mark.asyncio
async def test_write_file_round_trip(sandbox):
    """write_file() should write content readable by read_file()."""
    test_content = "# Integration test\nprint('sandbox write_file works!')\n"
    test_path = "/repo/autodev_integration_test.py"

    await sandbox.write_file(test_path, test_content)

    # Read back and verify
    read_back = await sandbox.read_file(test_path)
    assert read_back == test_content, (
        f"Round-trip mismatch.\nExpected: {test_content!r}\nGot: {read_back!r}"
    )

    # Clean up
    await sandbox.delete_file(test_path)
    assert not await sandbox.file_exists(test_path), "File not deleted"


@pytest.mark.asyncio
async def test_write_file_unicode(sandbox):
    """write_file() should handle unicode content correctly."""
    content = "# Unicode test: αβγ 日本語 🚀\nname = 'Ångström'\n"
    path = "/repo/autodev_unicode_test.py"

    await sandbox.write_file(path, content)
    read_back = await sandbox.read_file(path)
    assert read_back == content
    await sandbox.delete_file(path)


@pytest.mark.asyncio
async def test_install_dependencies(sandbox):
    """install_dependencies() should detect and install repo deps."""
    result = await sandbox.install_dependencies()
    # Should succeed (exit 0) even if no deps to install
    assert result.exit_code == 0, (
        f"Dependency install failed (exit {result.exit_code}):\n{result.combined}"
    )


@pytest.mark.asyncio
async def test_run_tests(sandbox):
    """run_tests() should return a structured TestResult."""
    from sandbox.parser import TestResult, TestStatus

    # First install deps
    await sandbox.install_dependencies()

    result = await sandbox.run_tests()

    assert isinstance(result, TestResult), "run_tests() should return TestResult"
    assert result.raw_output, "raw_output should be populated"

    # We don't assert pass/fail — just that the suite *ran*
    assert result.total >= 0, "total tests should be >= 0"
    assert result.status in (
        TestStatus.PASSED, TestStatus.FAILED, TestStatus.ERROR, TestStatus.UNKNOWN
    )

    print(f"\n[Integration] Test result: {result.status.value}")
    print(f"  Passed:  {result.passed}")
    print(f"  Failed:  {result.failed}")
    print(f"  Skipped: {result.skipped}")
    print(f"  Total:   {result.total}")


@pytest.mark.asyncio
async def test_repo_metadata(sandbox):
    """get_repo_metadata() should return structured language/framework info."""
    meta = await sandbox.get_repo_metadata()

    assert meta.primary_language != "unknown", \
        f"Could not detect primary language. top extensions unknown."
    assert meta.total_files > 0
    assert isinstance(meta.dep_files, list)
    assert isinstance(meta.test_dirs, list)

    print(f"\n[Integration] Repo metadata:")
    print(f"  Language:       {meta.primary_language}")
    print(f"  Test framework: {meta.test_framework}")
    print(f"  Dep files:      {meta.dep_files}")
    print(f"  Test dirs:      {meta.test_dirs}")
    print(f"  Total files:    {meta.total_files}")


@pytest.mark.asyncio
async def test_run_command_timeout():
    """
    A command that exceeds its timeout should return exit_code=124.
    Uses a fresh minimal sandbox (not the module-scoped fixture).
    """
    from sandbox.manager import SandboxManager
    from sandbox.config import SandboxConfig

    # We only need a plain container — no repo cloning
    cfg = SandboxConfig(
        network=os.environ.get("SANDBOX_NETWORK", "bridge"),
        command_timeout=2,  # very short
    )
    # Use a bare alpine image to avoid git bootstrap overhead
    cfg.image = "alpine:3.20"

    sb = SandboxManager(repo_url="https://github.com/x/y", config=cfg)
    await sb.start()
    try:
        result = await sb._exec(["sleep", "30"], timeout=2)
        assert result.exit_code == 124, \
            f"Expected timeout exit code 124, got {result.exit_code}"
        assert "timed out" in result.stderr.lower() or result.exit_code == 124
    finally:
        await sb.destroy()
