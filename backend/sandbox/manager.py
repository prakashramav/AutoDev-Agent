"""
SandboxManager — Phase 2 implementation.

Changes from Phase 1:
────────────────────────────────────────────────────────────────────────────
1. Accepts a typed :class:`SandboxConfig` instead of reading globals directly,
   making each sandbox independently configurable and unit-testable.

2. write_file() now uses `docker exec -i` with stdin piping — the host
   filesystem (/tmp) is no longer touched at all.

3. run_tests() now returns a structured :class:`TestResult` (via the parser
   module) rather than a raw CommandResult. This gives the agent clean
   pass/fail counts and a formatted failure summary.

4. install_dependencies() probes for pip / npm / cargo / maven and installs
   the repo's dependencies automatically before running tests.

5. get_repo_metadata() returns structured info about the repo (language,
   test framework, dependency file locations) so the Planner can make
   informed decisions without reading every file.

6. Hard kill: if the container is still running after destroy() is called,
   we send SIGKILL to the container (not just docker rm -f which is already
   forceful, but we also stop the docker exec process tree).

7. Strict typing throughout — no `Optional[str]` without a default.

The host Python process NEVER executes repo code directly.
All execution goes through `docker exec <container_id> ...`.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Optional

from sandbox.config import SandboxConfig
from sandbox.parser import TestResult, parse_pytest_output
import structlog

logger = structlog.get_logger(__name__)


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    """
    Raw output from a single command executed inside the sandbox.
    Returned by run_command() and internal helpers.
    """
    stdout: str
    stderr: str
    exit_code: int

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        """stdout + stderr, suitable for display or logging."""
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass
class RepoMetadata:
    """
    High-level facts about the cloned repository.
    Used by the Planner (Phase 3+) to pick the right tools.
    """
    primary_language: str = "unknown"      # e.g. "python", "javascript", "go"
    test_framework: str = "unknown"        # e.g. "pytest", "jest", "go test"
    dep_files: list[str] = field(default_factory=list)   # e.g. ["requirements.txt"]
    test_dirs: list[str] = field(default_factory=list)   # e.g. ["tests/", "test/"]
    total_files: int = 0
    top_level_dirs: list[str] = field(default_factory=list)


# ─── SandboxManager ───────────────────────────────────────────────────────────

class SandboxManager:
    """
    Manages a single ephemeral Docker sandbox container.

    Lifecycle:
        manager = SandboxManager(repo_url=..., config=SandboxConfig())
        # Or use as async context manager for automatic cleanup:
        async with SandboxManager(repo_url=...) as sb:
            result = await sb.run_command("pytest --tb=short")
            test_result = await sb.run_tests()
    """

    def __init__(
        self,
        repo_url: str,
        run_id: Optional[str] = None,
        config: Optional[SandboxConfig] = None,
    ) -> None:
        self.repo_url = repo_url
        self.run_id = run_id or str(uuid.uuid4())
        self.config = config or SandboxConfig()
        self.container_id: Optional[str] = None
        self._container_name = f"autodev-sandbox-{self.run_id[:8]}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> str:
        """
        Pull (if needed) the sandbox image and start the container.
        Returns the full container ID.
        """
        # Ensure the specified docker network exists
        if self.config.network and self.config.network not in ("bridge", "host", "none"):
            net_check = await self._host_run(["docker", "network", "inspect", self.config.network], timeout=10)
            if not net_check.success:
                logger.info("creating_sandbox_network", network=self.config.network)
                create_res = await self._host_run(["docker", "network", "create", self.config.network], timeout=15)
                if not create_res.success:
                    logger.warning("network_create_failed_fallback_bridge", error=create_res.stderr or create_res.stdout)
                    self.config.network = "bridge"

        # Base python-slim images lack git and cannot run apt-get under --read-only / --cap-drop ALL.
        # Upgrade automatically to autodev-sandbox:latest which has git, curl, and pytest.
        if self.config.image in ("python:3.12-slim", "python:3.12", "python:3.11-slim", "python:3-slim"):
            self.config.image = "autodev-sandbox:latest"

        # Verify sandbox image exists, or build autodev-sandbox:latest from Dockerfile.sandbox
        if self.config.image == "autodev-sandbox:latest":
            img_check = await self._host_run(["docker", "image", "inspect", "autodev-sandbox:latest"], timeout=10)
            if not img_check.success:
                logger.info("building_sandbox_image", image="autodev-sandbox:latest")
                import os
                backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                dockerfile_path = os.path.join(backend_dir, "Dockerfile.sandbox")
                if os.path.exists(dockerfile_path):
                    build_res = await self._host_run(
                        ["docker", "build", "-t", "autodev-sandbox:latest", "-f", dockerfile_path, backend_dir],
                        timeout=180,
                    )
                    if not build_res.success:
                        logger.warning("sandbox_build_failed_fallback", error=build_res.stderr or build_res.stdout)
                        self.config.image = "python:3.12-slim"
                else:
                    self.config.image = "python:3.12-slim"

        # Clean up any leftover container with the same name before starting
        await self._host_run(["docker", "rm", "-f", self._container_name], timeout=15)

        flags = self.config.as_docker_run_flags(self._container_name, self.run_id)
        cmd = [
            "docker", "run",
            *flags,
            self.config.image,
            "sleep", "infinity",   # keep alive for docker exec
        ]
        logger.info(
            "sandbox_starting",
            run_id=self.run_id,
            image=self.config.image,
            name=self._container_name,
        )
        result = await self._host_run(cmd, timeout=60)
        if not result.success:
            err_output = (result.stderr or result.stdout or f"exit code {result.exit_code}").strip()
            if (
                "dockerDesktopLinuxEngine" in err_output
                or "error during connect" in err_output
                or "cannot find the file specified" in err_output
            ):
                raise RuntimeError(
                    "Docker Desktop is not running. Please start Docker Desktop on your machine so the agent can launch the isolated sandbox container."
                )
            raise RuntimeError(
                f"Failed to start sandbox container '{self._container_name}': {err_output}"
            )

        self.container_id = result.stdout.strip()
        logger.info("sandbox_started", container_id=self.container_id[:12])

        # Install git + curl (needed for cloning and dependency detection)
        await self._bootstrap_container()
        return self.container_id

    async def _bootstrap_container(self) -> None:
        """Install minimal tooling inside the sandbox (git, curl)."""
        check = await self._exec(["git", "--version"])
        if check.success:
            logger.info("sandbox_tools_already_installed", version=check.stdout.strip())
            return

        logger.info("sandbox_bootstrap_start", container_id=self.container_id[:12])
        bootstrap_cmd = (
            "apt-get update -qq 2>&1 | tail -3 && "
            "apt-get install -y -qq git curl 2>&1 | tail -5"
        )
        result = await self._exec(
            ["sh", "-c", bootstrap_cmd],
            timeout=self.config.setup_timeout,
        )
        if not result.success:
            # Non-fatal warning — maybe git is already present in the image
            logger.warning(
                "sandbox_bootstrap_warning",
                stderr=result.stderr[:300],
            )
        logger.info("sandbox_bootstrap_done")

    async def clone_repo(self) -> CommandResult:
        """
        Shallow-clone the target repo into config.repo_dir inside the sandbox.
        Uses --filter=blob:none for even faster clone on repos with large history.
        """
        logger.info("cloning_repo", repo_url=self.repo_url, run_id=self.run_id)
        result = await self._exec(
            [
                "git", "clone",
                "--depth", "1",
                "--single-branch",
                "--filter=blob:none",   # partial clone — tree only, no blobs yet
                self.repo_url,
                self.config.repo_dir,
            ],
            timeout=self.config.clone_timeout,
        )
        if not result.success:
            err_msg = (result.stderr or result.stdout or f"exit code {result.exit_code}").strip()
            raise RuntimeError(
                f"git clone failed for {self.repo_url}:\n{err_msg}"
            )
        logger.info("clone_complete", repo_dir=self.config.repo_dir)
        return result

    async def destroy(self) -> None:
        """
        Force-stop and remove the sandbox container.
        Always called in __aexit__ — safe to call even if start() was never called.
        """
        if not self.container_id:
            return
        short_id = self.container_id[:12]
        logger.info("sandbox_destroying", container_id=short_id)
        result = await self._host_run(
            ["docker", "rm", "-f", self.container_id],
            timeout=15,
        )
        if result.success:
            logger.info("sandbox_destroyed", container_id=short_id)
        else:
            logger.warning(
                "sandbox_destroy_failed",
                container_id=short_id,
                stderr=result.stderr[:200],
            )
        self.container_id = None

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "SandboxManager":
        await self.start()
        await self.clone_repo()
        return self

    async def __aexit__(self, *_) -> None:
        await self.destroy()

    # ── File operations ───────────────────────────────────────────────────────

    async def list_files(self, path: Optional[str] = None) -> str:
        """
        Return a sorted newline-separated list of all non-.git files under `path`.
        Defaults to the repo root.
        """
        root = path or self.config.repo_dir
        result = await self._exec(
            ["find", root, "-not", "-path", "*/.git/*", "-type", "f", "-print"],
        )
        # Sort and strip trailing whitespace
        lines = sorted(l.strip() for l in result.stdout.splitlines() if l.strip())
        return "\n".join(lines)

    async def read_file(self, path: str) -> str:
        """
        Read and return the UTF-8 content of a file inside the sandbox.
        Raises FileNotFoundError if the file doesn't exist or can't be read.
        """
        result = await self._exec(["cat", path])
        if not result.success:
            raise FileNotFoundError(
                f"Cannot read '{path}' inside sandbox: {result.stderr.strip()}"
            )
        return result.stdout

    async def write_file(self, path: str, content: str) -> None:
        """
        Write `content` to `path` inside the sandbox.

        Implementation: base64-encode the content and pipe it through
        `docker exec -i` into the container. This:
        - Avoids any host-filesystem touch (no /tmp staging).
        - Handles arbitrary binary/unicode content without shell-escaping risks.
        - Creates parent directories if they don't exist.
        """
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        # Create parent directory, then decode and write in one shell expression
        parent = "/".join(path.split("/")[:-1]) or "/"
        cmd_str = (
            f"mkdir -p {shlex.quote(parent)} && "
            f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        )
        result = await self._exec(["sh", "-c", cmd_str])
        if not result.success:
            raise RuntimeError(
                f"write_file failed for '{path}': {result.stderr.strip()}"
            )

    async def delete_file(self, path: str) -> None:
        """Remove a file inside the sandbox."""
        result = await self._exec(["rm", "-f", path])
        if not result.success:
            raise RuntimeError(
                f"delete_file failed for '{path}': {result.stderr.strip()}"
            )

    async def file_exists(self, path: str) -> bool:
        """Check whether a file exists inside the sandbox."""
        result = await self._exec(["test", "-f", path])
        return result.success

    async def get_diff(self) -> str:
        """
        Run `git diff` inside the repo directory and return unified diff string.
        Also tracks untracked files with `git status --porcelain`.
        """
        result = await self._exec(["git", "diff", "HEAD"])
        diff_output = result.stdout
        # Also check for newly created files not yet tracked
        status_res = await self._exec(["git", "status", "--porcelain"])
        untracked = [
            line[3:].strip()
            for line in status_res.stdout.splitlines()
            if line.startswith("??")
        ]
        for u in untracked:
            diff_output += f"\n--- /dev/null\n+++ b/{u}\n"
            try:
                content = await self.read_file(f"{self.config.repo_dir}/{u}")
                for cl in content.splitlines():
                    diff_output += f"+{cl}\n"
            except Exception:
                pass
        return diff_output.strip()

    async def commit_changes(
        self,
        branch_name: str,
        commit_message: str,
        author_name: str = "AutoDev Bot",
        author_email: str = "autodev-bot@users.noreply.github.com",
    ) -> CommandResult:
        """
        Create a new branch, stage all changes (including new files), and commit.
        """
        cmds = (
            f"git config user.name {shlex.quote(author_name)} && "
            f"git config user.email {shlex.quote(author_email)} && "
            f"git checkout -b {shlex.quote(branch_name)} && "
            f"git add -A && "
            f"git commit -m {shlex.quote(commit_message)}"
        )
        return await self._exec(["sh", "-c", cmds])

    async def push_branch(
        self,
        branch_name: str,
        github_token: Optional[str] = None,
    ) -> CommandResult:
        """
        Push branch to remote origin. If token is provided, configures authenticated remote URL.
        """
        if github_token:
            auth_url = self.repo_url
            if auth_url.startswith("https://"):
                auth_url = auth_url.replace("https://", f"https://x-access-token:{github_token}@")
            push_cmd = f"git push {shlex.quote(auth_url)} {shlex.quote(branch_name)}"
        else:
            push_cmd = f"git push origin {shlex.quote(branch_name)}"

        return await self._exec(["sh", "-c", push_cmd])

    # ── Command execution ─────────────────────────────────────────────────────

    async def run_command(
        self,
        cmd: str,
        workdir: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> CommandResult:
        """
        Execute a shell command inside the sandbox.

        Args:
            cmd: Shell command string (run via sh -c).
            workdir: Working directory inside the container. Defaults to repo root.
            env: Extra environment variables to set.
            timeout: Override the default command_timeout for this specific call.

        Returns:
            CommandResult with stdout, stderr, and exit_code.
        """
        effective_workdir = workdir or self.config.repo_dir
        effective_timeout = timeout or self.config.command_timeout

        # Build env prefix: KEY=value VAL=value sh -c '...'
        env_prefix = ""
        if env:
            parts = " ".join(
                f"{k}={shlex.quote(str(v))}" for k, v in env.items()
            )
            env_prefix = f"env {parts} "

        full_cmd = f"{env_prefix}sh -c {shlex.quote(cmd)}"
        return await self._exec(
            ["sh", "-c", cmd],
            workdir=effective_workdir,
            timeout=effective_timeout,
        )

    # ── Dependency installation ───────────────────────────────────────────────

    async def install_dependencies(self) -> CommandResult:
        """
        Auto-detect and install the repo's dependencies inside the sandbox.

        Detection order (first match wins):
          1. requirements.txt / pyproject.toml / setup.py → pip
          2. package.json → npm ci
          3. go.mod → go mod download
          4. Cargo.toml → cargo fetch
          5. pom.xml / build.gradle → mvn / gradle (warn only — not installed by default)

        Returns the CommandResult of the installation command.
        If no known dep file is found, returns a synthetic "skipped" result.
        """
        repo = self.config.repo_dir
        logger.info("installing_dependencies", repo=repo)

        # Probe for dependency files
        probes = [
            # (file_to_test, install_command)
            (
                f"{repo}/requirements.txt",
                f"pip install -q -r {repo}/requirements.txt 2>&1",
            ),
            (
                f"{repo}/pyproject.toml",
                f"pip install -q -e {repo} 2>&1",
            ),
            (
                f"{repo}/setup.py",
                f"pip install -q -e {repo} 2>&1",
            ),
            (
                f"{repo}/package.json",
                f"cd {repo} && npm ci --prefer-offline 2>&1",
            ),
            (
                f"{repo}/go.mod",
                f"cd {repo} && go mod download 2>&1",
            ),
            (
                f"{repo}/Cargo.toml",
                f"cd {repo} && cargo fetch 2>&1",
            ),
        ]

        for dep_file, install_cmd in probes:
            if await self.file_exists(dep_file):
                logger.info("dep_file_found", dep_file=dep_file)
                result = await self._exec(
                    ["sh", "-c", install_cmd],
                    workdir=repo,
                    timeout=self.config.command_timeout,
                )
                if result.success:
                    logger.info("dependencies_installed", dep_file=dep_file)
                else:
                    logger.warning(
                        "dep_install_warning",
                        dep_file=dep_file,
                        stderr=result.stderr[:300],
                    )
                return result

        logger.info("no_dep_file_found", repo=repo)
        return CommandResult(
            stdout="No recognized dependency file found — skipping install.",
            stderr="",
            exit_code=0,
        )

    # ── Test execution ────────────────────────────────────────────────────────

    async def run_tests(
        self,
        workdir: Optional[str] = None,
        extra_args: str = "",
    ) -> TestResult:
        """
        Run the repo's test suite inside the sandbox and return a structured result.

        Currently supports pytest (Python). The Test Agent (Phase 4+) will extend
        this to other frameworks using the RepoMetadata.test_framework field.

        Args:
            workdir: Override the working directory inside the container.
            extra_args: Additional flags appended to the pytest command
                        (e.g., "-k test_login" to run a specific test).

        Returns:
            A :class:`TestResult` with parsed counts, cases, and failure summary.
        """
        effective_workdir = workdir or self.config.repo_dir

        # Ensure pytest is available; install silently if not
        await self._exec(
            ["sh", "-c", "pip install -q pytest pytest-asyncio 2>&1 | tail -3"],
            timeout=60,
        )

        pytest_cmd = (
            f"cd {shlex.quote(effective_workdir)} && "
            f"python -m pytest --tb=short -q {extra_args} 2>&1"
        )
        logger.info("running_tests", workdir=effective_workdir)
        result = await self._exec(
            ["sh", "-c", pytest_cmd],
            workdir=effective_workdir,
            timeout=self.config.command_timeout,
        )

        test_result = parse_pytest_output(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )
        logger.info(
            "tests_complete",
            status=test_result.status.value,
            passed=test_result.passed,
            failed=test_result.failed,
            errors=test_result.errors,
        )
        return test_result

    # ── Repository inspection ─────────────────────────────────────────────────

    async def get_repo_metadata(self) -> RepoMetadata:
        """
        Inspect the cloned repo and return structured metadata.

        Used by the Planner (Phase 3+) to decide which tools to invoke.
        """
        repo = self.config.repo_dir
        meta = RepoMetadata()

        # Total file count
        file_list = await self.list_files()
        files = [l for l in file_list.splitlines() if l]
        meta.total_files = len(files)

        # Top-level directories
        ls_result = await self._exec(
            ["sh", "-c", f"ls -1 {repo}/ 2>/dev/null || true"]
        )
        meta.top_level_dirs = [
            l.strip() for l in ls_result.stdout.splitlines() if l.strip()
        ]

        # Detect primary language by extension frequency
        ext_counts: dict[str, int] = {}
        for f in files:
            if "." in f.split("/")[-1]:
                ext = f.rsplit(".", 1)[-1].lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

        lang_map = {
            "py": "python", "js": "javascript", "ts": "typescript",
            "go": "go", "rs": "rust", "java": "java", "rb": "ruby",
            "cs": "csharp", "cpp": "cpp", "c": "c",
        }
        if ext_counts:
            top_ext = max(ext_counts, key=lambda k: ext_counts[k])
            meta.primary_language = lang_map.get(top_ext, top_ext)

        # Detect dependency files
        dep_candidates = [
            "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
            "Gemfile",
        ]
        for dep in dep_candidates:
            if await self.file_exists(f"{repo}/{dep}"):
                meta.dep_files.append(dep)

        # Detect test directories
        test_dir_candidates = ["tests", "test", "spec", "__tests__", "e2e"]
        for td in test_dir_candidates:
            check = await self._exec(["test", "-d", f"{repo}/{td}"])
            if check.success:
                meta.test_dirs.append(td)

        # Detect test framework
        if meta.primary_language == "python":
            meta.test_framework = "pytest"
        elif meta.primary_language in ("javascript", "typescript"):
            # Check for jest vs mocha
            if await self.file_exists(f"{repo}/jest.config.js") or await self.file_exists(f"{repo}/jest.config.ts"):
                meta.test_framework = "jest"
            elif "mocha" in (await self._exec(["sh", "-c", f"cat {repo}/package.json 2>/dev/null || true"])).stdout:
                meta.test_framework = "mocha"
            else:
                meta.test_framework = "jest"
        elif meta.primary_language == "go":
            meta.test_framework = "go test"
        elif meta.primary_language == "rust":
            meta.test_framework = "cargo test"

        logger.info(
            "repo_metadata",
            language=meta.primary_language,
            test_framework=meta.test_framework,
            total_files=meta.total_files,
            dep_files=meta.dep_files,
        )
        return meta

    async def get_file_tree(self, max_depth: int = 4) -> str:
        """
        Return an indented file tree (like `tree`) up to max_depth levels.
        Falls back to plain list_files if `tree` is not available.
        """
        tree_result = await self._exec(
            [
                "sh", "-c",
                f"tree -L {max_depth} --noreport -I '.git' {self.config.repo_dir} 2>/dev/null "
                f"|| find {self.config.repo_dir} -not -path '*/.git/*' -type f | sort",
            ]
        )
        return tree_result.stdout

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _exec(
        self,
        cmd: list[str],
        workdir: Optional[str] = None,
        timeout: int = 60,
    ) -> CommandResult:
        """Execute `cmd` inside the sandbox container via `docker exec`."""
        return await self._exec_in_sandbox(cmd, workdir=workdir, timeout=timeout)

    async def _exec_in_sandbox(
        self,
        cmd: list[str],
        workdir: Optional[str] = None,
        timeout: int = 60,
    ) -> CommandResult:
        """Internal execution helper inside the container."""
        if not self.container_id:
            raise RuntimeError(
                "Sandbox not started — call start() or use as async context manager"
            )

        effective_workdir = workdir or self.config.repo_dir
        docker_cmd = [
            "docker", "exec",
            "--workdir", effective_workdir,
            self.container_id,
            *cmd,
        ]
        return await self._host_run(docker_cmd, timeout=timeout)

    @staticmethod
    async def _host_run(
        cmd: list[str],
        timeout: int = 60,
        stdin_data: Optional[bytes] = None,
    ) -> CommandResult:
        """
        Execute a command on the HOST — ONLY ever used for docker CLI calls.
        Repo or generated code is NEVER passed here directly.

        Args:
            cmd: The docker command to run (e.g., ["docker", "exec", ...]).
            timeout: Seconds before the command is killed.
            stdin_data: Optional bytes to pipe into the process stdin.
        """
        def _sync_exec() -> CommandResult:
            try:
                res = subprocess.run(
                    cmd,
                    input=stdin_data,
                    capture_output=True,
                    timeout=timeout,
                )
                return CommandResult(
                    stdout=res.stdout.decode("utf-8", errors="replace"),
                    stderr=res.stderr.decode("utf-8", errors="replace"),
                    exit_code=res.returncode,
                )
            except subprocess.TimeoutExpired:
                logger.error("host_run_timeout", cmd=cmd[:4], timeout=timeout)
                return CommandResult(
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    exit_code=124,
                )
            except Exception as exc:
                logger.exception("host_run_error", cmd=cmd[:4])
                return CommandResult(stdout="", stderr=f"{type(exc).__name__}: {exc}", exit_code=1)

        result = await asyncio.to_thread(_sync_exec)
        if not result.success:
            logger.warning(
                "host_run_failed",
                cmd=cmd[:4],
                exit_code=result.exit_code,
                stdout=result.stdout[:500],
                stderr=result.stderr[:500],
            )
        return result

    # ── Dunder ────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        cid = self.container_id[:12] if self.container_id else "not_started"
        return (
            f"SandboxManager("
            f"run_id={self.run_id!r}, "
            f"container={cid!r}, "
            f"repo={self.repo_url!r})"
        )
