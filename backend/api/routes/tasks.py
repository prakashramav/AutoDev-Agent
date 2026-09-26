"""
POST /tasks — submit a new agent run.
GET  /tasks/{run_id} — get run status & outputs.
GET  /tasks — list recent runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from core.database import get_db
from models.run import Run, RunStatus
from worker.task_runner import run_task

logger = logging.getLogger(__name__)
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
    await db.flush()   # get the id before background task starts
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
