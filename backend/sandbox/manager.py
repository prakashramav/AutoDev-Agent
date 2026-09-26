"""
SandboxManager — Phase 1 implementation.

Responsibilities:
- Spin up an ephemeral Docker container from a base image.
- Clone the target repo inside the container (never on the host).
- Expose read_file / list_files for Phase 1 inspection.
- Expose run_command / write_file / run_tests for Phase 2+.
- Destroy the container at the end of a run.

The host Python process NEVER executes repo code directly.
All execution goes through `docker exec <container_id> ...`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import uuid
from dataclasses import dataclass, field
from typing import Optional

from core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class SandboxManager:
    """
    Manages an ephemeral Docker sandbox container for a single task run.

    Usage (async context manager):

        async with SandboxManager(repo_url="https://github.com/...") as sb:
            result = await sb.run_command("ls -la /repo")
            content = await sb.read_file("/repo/README.md")
    """
    repo_url: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    container_id: Optional[str] = field(default=None, init=False)
    _workdir: str = field(default="/repo", init=False)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> str:
        """
        Create the sandbox container and return its container ID.
        The container is started detached (sleeping) so we can docker exec into it.
        """
        container_name = f"autodev-sandbox-{self.run_id[:8]}"
        cmd = [
            "docker", "run",
            "--detach",
            "--name", container_name,
            # Resource limits
            f"--cpu-quota={settings.SANDBOX_CPU_QUOTA}",
            f"--memory={settings.SANDBOX_MEMORY_LIMIT}",
            # Network isolation — containers can reach GitHub/pip but not host services
            f"--network={settings.SANDBOX_NETWORK}",
            # Security hardening
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "--tmpfs", "/repo:rw,size=1g",
            # Labels for easy cleanup
            "--label", "autodev=true",
            "--label", f"run_id={self.run_id}",
            settings.SANDBOX_IMAGE,
            # Keep alive: sleep forever until we exec into it
            "sleep", "infinity",
        ]

        logger.info("starting_sandbox", run_id=self.run_id, image=settings.SANDBOX_IMAGE)
        result = await self._host_run(cmd)
        if not result.success:
            raise RuntimeError(
                f"Failed to start sandbox container: {result.stderr}"
            )

        self.container_id = result.stdout.strip()
        logger.info("sandbox_started", container_id=self.container_id[:12])

        # Install git inside the sandbox (slim images don't have it)
        await self._exec_in_sandbox(
            ["sh", "-c", "apt-get update -qq && apt-get install -y -qq git 2>&1"],
            timeout=120,
        )
        return self.container_id

    async def clone_repo(self) -> CommandResult:
        """Clone the target repo into /repo inside the sandbox."""
        logger.info("cloning_repo", repo_url=self.repo_url)
        result = await self._exec_in_sandbox(
            [
                "git", "clone",
                "--depth", "1",          # shallow clone — faster, less surface area
                "--single-branch",
                self.repo_url,
                self._workdir,
            ],
            timeout=settings.SANDBOX_TIMEOUT_SECONDS,
        )
        if not result.success:
            raise RuntimeError(f"git clone failed: {result.stderr}")
        logger.info("clone_complete", exit_code=result.exit_code)
        return result

    async def destroy(self) -> None:
        """Stop and remove the sandbox container. Always called in __aexit__."""
        if not self.container_id:
            return
        logger.info("destroying_sandbox", container_id=self.container_id[:12])
        await self._host_run(["docker", "rm", "-f", self.container_id])
        self.container_id = None

    # ── Context-manager helpers ──────────────────────────────────

    async def __aenter__(self) -> "SandboxManager":
        await self.start()
        await self.clone_repo()
        return self

    async def __aexit__(self, *_) -> None:
        await self.destroy()

    # ── Public API (Phase 1 subset) ──────────────────────────────

    async def list_files(self, path: str = "/repo") -> str:
        """Return a text tree of all files under `path`."""
        result = await self._exec_in_sandbox(
            ["find", path, "-not", "-path", "*/.git/*", "-type", "f"],
        )
        return result.stdout

    async def read_file(self, path: str) -> str:
        """Read and return the content of a file inside the sandbox."""
        result = await self._exec_in_sandbox(["cat", path])
        if not result.success:
            raise FileNotFoundError(
                f"Cannot read {path} in sandbox: {result.stderr}"
            )
        return result.stdout

    async def run_command(self, cmd: str, workdir: str = "/repo") -> CommandResult:
        """
        Run an arbitrary shell command inside the sandbox.
        `cmd` is a shell string (passed via sh -c) — use with caution.
        """
        return await self._exec_in_sandbox(
            ["sh", "-c", cmd],
            workdir=workdir,
            timeout=settings.SANDBOX_TIMEOUT_SECONDS,
        )

    async def write_file(self, path: str, content: str) -> None:
        """
        Write `content` to `path` inside the sandbox.
        Pipes via stdin to avoid shell-injection from content.
        """
        # Use docker cp approach: write to a tmp file on the host, then docker cp.
        # This avoids any shell escaping issues with content.
        # We write to /tmp on the host (not repo code), so it's safe.
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = f.name
        try:
            result = await self._host_run(
                ["docker", "cp", tmp_path, f"{self.container_id}:{path}"]
            )
            if not result.success:
                raise RuntimeError(f"write_file failed for {path}: {result.stderr}")
        finally:
            os.unlink(tmp_path)

    async def run_tests(self, workdir: str = "/repo") -> CommandResult:
        """
        Run the repo's test suite via pytest.
        Returns stdout/stderr and exit code.
        Phase 2+ — already wired so Phase 4 can call it seamlessly.
        """
        return await self._exec_in_sandbox(
            [
                "sh", "-c",
                (
                    "pip install -q pytest pytest-asyncio 2>&1 | tail -5 && "
                    f"cd {workdir} && "
                    "python -m pytest --tb=short -q 2>&1"
                ),
            ],
            workdir=workdir,
            timeout=settings.SANDBOX_TIMEOUT_SECONDS,
        )

    # ── Internal helpers ─────────────────────────────────────────

    async def _exec_in_sandbox(
        self,
        cmd: list[str],
        workdir: str = "/repo",
        timeout: int = 60,
    ) -> CommandResult:
        """Run `cmd` via `docker exec` inside this container."""
        if not self.container_id:
            raise RuntimeError("Sandbox not started — call start() first")

        docker_cmd = [
            "docker", "exec",
            "--workdir", workdir,
            self.container_id,
            *cmd,
        ]
        return await self._host_run(docker_cmd, timeout=timeout)

    @staticmethod
    async def _host_run(
        cmd: list[str],
        timeout: int = 60,
    ) -> CommandResult:
        """
        Execute a command on the HOST (used only to talk to the docker daemon).
        Repo/generated code is NEVER run here — only docker CLI calls.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return CommandResult(
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
            )
        except asyncio.TimeoutError:
            logger.error("sandbox_command_timeout", cmd=cmd[:3])
            try:
                proc.kill()
            except Exception:
                pass
            return CommandResult(stdout="", stderr="Timeout exceeded", exit_code=124)
        except Exception as exc:
            logger.exception("sandbox_command_error", cmd=cmd[:3])
            return CommandResult(stdout="", stderr=str(exc), exit_code=1)
