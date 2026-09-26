"""
SandboxConfig — typed, validated configuration for a sandbox container.

Separating config from the manager makes each independently testable and
lets callers override specific limits without touching global settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.config import settings


@dataclass
class SandboxConfig:
    """
    Resource and security configuration for an ephemeral sandbox container.

    All values have sensible defaults drawn from the application Settings,
    so callers only need to override what they care about.
    """

    # ── Base image ─────────────────────────────────────────────────
    image: str = field(default_factory=lambda: settings.SANDBOX_IMAGE)

    # ── Resource limits ────────────────────────────────────────────
    # --cpu-quota: microseconds per 100ms period the container can use.
    # 50 000 == 50 % of one CPU core.
    cpu_quota: int = field(default_factory=lambda: settings.SANDBOX_CPU_QUOTA)

    # --memory: hard memory limit (e.g., "512m", "1g")
    memory_limit: str = field(default_factory=lambda: settings.SANDBOX_MEMORY_LIMIT)

    # --pids-limit: cap number of processes inside the container
    pids_limit: int = 64

    # ── Timeouts ───────────────────────────────────────────────────
    # Per-command timeout (seconds) used in docker exec calls
    command_timeout: int = field(
        default_factory=lambda: settings.SANDBOX_TIMEOUT_SECONDS
    )

    # Timeout for the initial container setup (apt-get, git install, etc.)
    setup_timeout: int = 180

    # Timeout for git clone (large repos can be slow on first clone)
    clone_timeout: int = 120

    # ── Filesystem ─────────────────────────────────────────────────
    # Where the cloned repo lives inside the container
    repo_dir: str = "/repo"

    # Size of the tmpfs mount for the working directory
    repo_tmpfs_size: str = "1g"

    # Size of /tmp inside the container
    tmp_tmpfs_size: str = "256m"

    # ── Network ────────────────────────────────────────────────────
    # The isolated Docker network the sandbox is attached to.
    # Network-level egress filtering (allowlist GitHub/pip/npm) is
    # configured at the Docker host via setup_sandbox_network.sh.
    network: str = field(default_factory=lambda: settings.SANDBOX_NETWORK)

    # ── Security ───────────────────────────────────────────────────
    # Drop ALL Linux capabilities; add back only what's strictly needed.
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    cap_add: list[str] = field(default_factory=list)   # none by default

    # Prevent privilege escalation inside the container
    no_new_privileges: bool = True

    # Run container filesystem as read-only; writable paths use tmpfs above
    read_only: bool = True

    def as_docker_run_flags(self, container_name: str, run_id: str) -> list[str]:
        """
        Render the config as CLI flags for `docker run`.

        Returns a flat list ready to be spliced into a docker run command:
            docker run [flags] <image> sleep infinity
        """
        flags: list[str] = [
            "--detach",
            "--name", container_name,
            # Resource limits
            f"--cpu-quota={self.cpu_quota}",
            f"--memory={self.memory_limit}",
            f"--pids-limit={self.pids_limit}",
            # Network
            f"--network={self.network}",
            # Filesystem
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={self.tmp_tmpfs_size}",
            "--tmpfs", f"{self.repo_dir}:rw,size={self.repo_tmpfs_size}",
            # Labels for observability / cleanup
            "--label", "autodev=true",
            "--label", f"run_id={run_id}",
        ]

        # Security hardening
        for cap in self.cap_drop:
            flags += ["--cap-drop", cap]
        for cap in self.cap_add:
            flags += ["--cap-add", cap]
        if self.no_new_privileges:
            flags += ["--security-opt", "no-new-privileges"]

        return flags
