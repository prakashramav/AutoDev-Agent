"""
`runs` table — one row per agent task run.

Tracks: repo, issue, lifecycle status, and the full audit trail.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import Column, DateTime, Integer, String, Text, JSON
from sqlalchemy import Enum as SAEnum

from core.database import Base


class RunStatus(str, PyEnum):
    """Lifecycle states for a single agent run."""
    PENDING       = "pending"        # job submitted, not yet picked up by worker
    CLONING       = "cloning"        # sandbox container spinning up, git clone running
    INSPECTING    = "inspecting"     # listing / reading files in the sandbox
    PLANNING      = "planning"       # LLM planning pass (Phase 3+)
    MODIFYING     = "modifying"      # Code Agent applying changes (Phase 4+)
    TESTING       = "testing"        # Test Agent running test suite (Phase 4+)
    REVIEWING     = "reviewing"      # Review Agent security pass (Phase 5+)
    CREATING_PR   = "creating_pr"    # Pushing branch and opening PR (Phase 6+)
    DONE          = "done"           # PR created successfully
    FAILED        = "failed"         # Unrecoverable error
    AWAITING_CONF = "awaiting_confirmation"  # dev-mode gate before GH write ops


class Run(Base):
    """Represents a single end-to-end agent run for a given issue."""
    __tablename__ = "runs"

    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    repo_url    = Column(String(512), nullable=False)
    issue_number = Column(Integer, nullable=True)   # numeric GitHub issue #
    issue_text   = Column(Text, nullable=True)      # raw issue text / title
    status       = Column(
        SAEnum(RunStatus, name="run_status"),
        nullable=False,
        default=RunStatus.PENDING,
    )

    # ── Outputs (progressively filled in) ──────────────────────
    sandbox_container_id = Column(String(128), nullable=True)  # docker container id
    file_tree            = Column(Text, nullable=True)          # repo file listing
    relevant_files       = Column(JSON, nullable=True)          # list[str]
    fix_plan             = Column(Text, nullable=True)          # LLM-generated plan
    diff                 = Column(Text, nullable=True)          # unified diff of changes
    test_results         = Column(JSON, nullable=True)          # test run output
    review_notes         = Column(Text, nullable=True)          # security review notes
    pr_url               = Column(String(512), nullable=True)   # final PR URL
    error_message        = Column(Text, nullable=True)          # set on FAILED

    # ── Full trace (append-only audit log for the run) ─────────
    # Each entry: {timestamp, type, data}
    trace = Column(JSON, nullable=True, default=list)

    # ── Timestamps ─────────────────────────────────────────────
    created_at  = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at  = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Run id={self.id!r} status={self.status!r} repo={self.repo_url!r}>"
