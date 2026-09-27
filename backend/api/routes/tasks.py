"""
POST /tasks — submit a new agent run.
GET  /tasks/{run_id} — get run status & outputs.
GET  /tasks — list recent runs.
"""
from __future__ import annotations

import structlog
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from core.database import get_db
from models.run import Run, RunStatus
from worker.task_runner import run_task

logger = structlog.get_logger(__name__)
router = APIRouter()


# ─── Request / Response schemas ────────────────────────────────

class TaskRequest(BaseModel):
    repo_url: str
    issue_number: Optional[int] = None
    issue_text: Optional[str] = None

    @model_validator(mode="after")
    def at_least_one_issue_field(self) -> "TaskRequest":
        if self.issue_number is None and not self.issue_text:
            raise ValueError(
                "Provide at least one of: issue_number, issue_text"
            )
        return self


class TaskResponse(BaseModel):
    run_id: str
    status: str
    repo_url: str
    issue_number: Optional[int]
    issue_text: Optional[str]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    # Progressive outputs
    sandbox_container_id: Optional[str] = None
    file_tree: Optional[str] = None
    relevant_files: Optional[list] = None
    fix_plan: Optional[str] = None
    diff: Optional[str] = None
    test_results: Optional[dict] = None
    review_notes: Optional[str] = None
    pr_url: Optional[str] = None
    error_message: Optional[str] = None
    trace: Optional[list] = None

    model_config = {"from_attributes": True}


def _run_to_response(run: Run) -> TaskResponse:
    return TaskResponse(
        run_id=run.id,
        status=run.status.value if isinstance(run.status, RunStatus) else run.status,
        repo_url=run.repo_url,
        issue_number=run.issue_number,
        issue_text=run.issue_text,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        sandbox_container_id=run.sandbox_container_id,
        file_tree=run.file_tree,
        relevant_files=run.relevant_files,
        fix_plan=run.fix_plan,
        diff=run.diff,
        test_results=run.test_results,
        review_notes=run.review_notes,
        pr_url=run.pr_url,
        error_message=run.error_message,
        trace=run.trace,
    )


# ─── Endpoints ─────────────────────────────────────────────────

@router.post("", response_model=TaskResponse, status_code=202)
async def submit_task(
    payload: TaskRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a new issue for the agent to work on.
    Returns immediately with a run_id; poll GET /tasks/{run_id} for progress.
    """
    run = Run(
        repo_url=payload.repo_url,
        issue_number=payload.issue_number,
        issue_text=payload.issue_text,
        status=RunStatus.PENDING,
        trace=[],
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    run_id = run.id
    logger.info("task_submitted", run_id=run_id, repo=payload.repo_url)

    # Enqueue the worker task as a FastAPI background task (Phase 1).
    # Phase 2+: replace with Redis queue / Celery / ARQ.
    background_tasks.add_task(run_task, run_id)

    return _run_to_response(run)


@router.get("/{run_id}", response_model=TaskResponse)
async def get_task(
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Poll for the status and outputs of a specific run."""
    result = await db.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return _run_to_response(run)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """List the most recent runs (newest first)."""
    result = await db.execute(
        select(Run).order_by(desc(Run.created_at)).limit(limit)
    )
    runs = result.scalars().all()
    return [_run_to_response(r) for r in runs]


@router.delete("/failed/cleanup", status_code=200)
@router.post("/failed/cleanup", status_code=200)
async def cleanup_failed_tasks(
    db: AsyncSession = Depends(get_db),
):
    """Delete all runs with failed status from database and cleanup any containers."""
    result = await db.execute(select(Run).where(Run.status == RunStatus.FAILED))
    failed_runs = result.scalars().all()
    count = len(failed_runs)
    for r in failed_runs:
        if r.sandbox_container_id:
            try:
                import subprocess
                subprocess.run(["docker", "rm", "-f", r.sandbox_container_id], capture_output=True, timeout=5)
            except Exception:
                pass
        await db.delete(r)
    await db.commit()
    logger.info("cleaned_up_failed_tasks", count=count)
    return {"deleted_count": count}


@router.post("/{run_id}/restart", response_model=TaskResponse)
@router.post("/{run_id}/retry", response_model=TaskResponse)
async def restart_task(
    run_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Restart or retry a run (e.g. failed or interrupted runs).
    Clears previous error messages and intermediate outputs, resets status to PENDING,
    and enqueues the worker task to execute with current configuration and sandbox tooling.
    """
    result = await db.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

    run.status = RunStatus.PENDING
    run.error_message = None
    run.completed_at = None
    run.sandbox_container_id = None
    run.file_tree = None
    run.relevant_files = None
    run.fix_plan = None
    run.diff = None
    run.test_results = None
    run.review_notes = None
    run.trace = (run.trace or []) + [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "run_restarted",
            "data": {"reason": "user_restart"},
        }
    ]
    run.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(run)

    logger.info("task_restarted", run_id=run.id, repo_url=run.repo_url)
    background_tasks.add_task(run_task, run.id)

    return _run_to_response(run)


@router.delete("/{run_id}", status_code=200)
@router.post("/{run_id}/delete", status_code=200)
async def delete_task(
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a run from the database and remove any leftover container."""
    result = await db.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

    if run.sandbox_container_id:
        try:
            import subprocess
            subprocess.run(["docker", "rm", "-f", run.sandbox_container_id], capture_output=True, timeout=5)
        except Exception:
            pass

    await db.delete(run)
    await db.commit()
    logger.info("task_deleted", run_id=run_id)
    return {"status": "deleted", "run_id": run_id}


