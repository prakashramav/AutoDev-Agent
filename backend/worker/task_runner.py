"""
Worker — Phase 2 task runner.

Core flow:
  1. Load the Run from Postgres.
  2. Spin up a SandboxManager with typed SandboxConfig.
  3. Clone the repo inside the sandbox.
  4. get_repo_metadata() — language, framework, dep files, test dirs.
  5. install_dependencies() inside the sandbox.
  6. list_files() + sample read to confirm file access.
  7. Persist all results + structured trace to the Run row.
  8. Destroy the sandbox (always, even on error).

Phase 3+: This file will grow to include the Planner LLM call.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import asdict

from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.run import Run, RunStatus
from sandbox.config import SandboxConfig
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

    sandbox: SandboxManager | None = None
    try:
        # ── Start sandbox ─────────────────────────────────────────
        await _update_run(run_id, status=RunStatus.CLONING)
        logger.info("cloning_repo", run_id=run_id, repo_url=repo_url)

        cfg = SandboxConfig()   # all values from settings; Phase 3+ can customize
        sandbox = SandboxManager(repo_url=repo_url, run_id=run_id, config=cfg)
        container_id = await sandbox.start()
        await _update_run(run_id, sandbox_container_id=container_id)

        clone_result = await sandbox.clone_repo()
        if not clone_result.success:
            raise RuntimeError(f"git clone failed:\n{clone_result.stderr}")

        # ── Phase 2: Inspect + metadata ──────────────────────────
        await _update_run(run_id, status=RunStatus.INSPECTING)
        logger.info("inspecting_repo", run_id=run_id)

        # Structured repo metadata (language, framework, dep files, test dirs)
        repo_meta = await sandbox.get_repo_metadata()
        file_tree = await sandbox.list_files()
        await _update_run(run_id, file_tree=file_tree)

        # ── Install dependencies ──────────────────────────────────
        dep_result = await sandbox.install_dependencies()
        logger.info(
            "dep_install_done",
            run_id=run_id,
            exit_code=dep_result.exit_code,
        )

        # ── Sample file read (confirm read access) ────────────────
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

        # ── Persist full inspection report ────────────────────────
        trace_entry = {
            "type": "inspection_complete",
            "data": {
                "total_files": repo_meta.total_files,
                "primary_language": repo_meta.primary_language,
                "test_framework": repo_meta.test_framework,
                "dep_files": repo_meta.dep_files,
                "test_dirs": repo_meta.test_dirs,
                "sample_file": sample_path,
                "sample_preview": (sample_content or "")[:500],
                "dep_install_exit_code": dep_result.exit_code,
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

        logger.info(
            "phase2_complete",
            run_id=run_id,
            total_files=repo_meta.total_files,
            language=repo_meta.primary_language,
            framework=repo_meta.test_framework,
        )

    except Exception as exc:
        logger.exception("worker_error", run_id=run_id)
        await _update_run(
            run_id,
            status=RunStatus.FAILED,
            error_message=str(exc),
        )
    finally:
        # Always destroy the sandbox container — even if an error occurred
        if sandbox is not None:
            try:
                await sandbox.destroy()
            except Exception:
                logger.warning("sandbox_destroy_failed_in_finally", run_id=run_id)


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
