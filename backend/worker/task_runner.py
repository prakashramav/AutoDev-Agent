"""
Worker — Phase 1 task runner.

Core flow:
  1. Load the Run from Postgres.
  2. Spin up a SandboxManager (creates a Docker container).
  3. Clone the repo inside the sandbox.
  4. List files and read a sample file to confirm read access.
  5. Persist results back to the Run row.
  6. Destroy the sandbox.

Phase 2+: This file will grow to include the full agent loop.
For now it is intentionally minimal so the infrastructure can be
validated end-to-end before adding LLM calls.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.run import Run, RunStatus
from sandbox.manager import SandboxManager

logger = logging.getLogger(__name__)


async def _append_trace(run: Run, entry: dict) -> None:
    """Append a structured entry to the run's trace log."""
    if run.trace is None:
        run.trace = []
    run.trace = run.trace + [
        {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    ]


async def _update_run(run_id: str, **kwargs) -> None:
    """Helper: open a short-lived session and update the Run row."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.error("run_not_found_in_worker", run_id=run_id)
            return
        for key, value in kwargs.items():
            setattr(run, key, value)
        run.updated_at = datetime.now(timezone.utc)
        if kwargs.get("status") in (RunStatus.DONE, RunStatus.FAILED):
            run.completed_at = datetime.now(timezone.utc)
        await session.commit()


async def run_task(run_id: str) -> None:
    """
    Entry point called by FastAPI BackgroundTasks (Phase 1).
    Executes the clone → inspect → report loop inside a sandbox.
    """
    logger.info("worker_starting", run_id=run_id)

    # ── Load run ────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.error("run_not_found", run_id=run_id)
            return
        repo_url = run.repo_url

    try:
        # ── Phase 1: Clone → Inspect ─────────────────────────────
        await _update_run(run_id, status=RunStatus.CLONING)
        logger.info("cloning_repo", run_id=run_id, repo_url=repo_url)

        sandbox = SandboxManager(repo_url=repo_url, run_id=run_id)
        container_id = await sandbox.start()
        await _update_run(run_id, sandbox_container_id=container_id)

        clone_result = await sandbox.clone_repo()
        if not clone_result.success:
            raise RuntimeError(f"git clone failed:\n{clone_result.stderr}")

        # ── Inspect: list files ──────────────────────────────────
        await _update_run(run_id, status=RunStatus.INSPECTING)
        logger.info("inspecting_repo", run_id=run_id)

        file_tree = await sandbox.list_files("/repo")
        await _update_run(run_id, file_tree=file_tree)

        # ── Read a sample file (README or first .py) to confirm access ──
        lines = [l.strip() for l in file_tree.splitlines() if l.strip()]
        sample_path: str | None = None
        for candidate in lines:
            if candidate.lower().endswith("readme.md"):
                sample_path = candidate
                break
        if not sample_path:
            for candidate in lines:
                if candidate.endswith(".py"):
                    sample_path = candidate
                    break
        if not sample_path and lines:
            sample_path = lines[0]

        sample_content: str | None = None
        if sample_path:
            try:
                sample_content = await sandbox.read_file(sample_path)
                logger.info(
                    "sample_file_read",
                    run_id=run_id,
                    path=sample_path,
                    chars=len(sample_content),
                )
            except Exception as exc:
                logger.warning("sample_read_failed", path=sample_path, exc=str(exc))

        # ── Persist full inspection report ───────────────────────
        trace_entry = {
            "type": "inspection_complete",
            "data": {
                "total_files": len(lines),
                "sample_file": sample_path,
                "sample_preview": (sample_content or "")[:500],
            },
        }
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Run).where(Run.id == run_id))
            run_obj = result.scalar_one()
            await _append_trace(run_obj, trace_entry)
            run_obj.status = RunStatus.DONE
            run_obj.updated_at = datetime.now(timezone.utc)
            run_obj.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info("phase1_complete", run_id=run_id, total_files=len(lines))

    except Exception as exc:
        logger.exception("worker_error", run_id=run_id)
        await _update_run(
            run_id,
            status=RunStatus.FAILED,
            error_message=str(exc),
        )
    finally:
        # Always destroy the sandbox container
        try:
            await sandbox.destroy()
        except Exception:
            pass


# ── Standalone entry point (Phase 1 worker process) ────────────
if __name__ == "__main__":
    """
    For Phase 1 the worker is started as a background task inside FastAPI.
    This __main__ block is a placeholder for Phase 2 when we switch to
    a Redis-backed job queue (ARQ / RQ / Celery).
    """
    import sys
    logging.basicConfig(level=logging.INFO)
    logger.info("worker_standalone_mode_not_yet_implemented")
    sys.exit(0)
