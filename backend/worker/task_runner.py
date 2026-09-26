"""
Worker — Phase 5 task runner using Multi-Agent Supervisor.

Runs the autonomous development loop through the LangGraph supervisor while
persisting step transitions and agent audit events to the database and frontend.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.run import Run, RunStatus
from sandbox.config import SandboxConfig
from sandbox.manager import SandboxManager
from agents.supervisor import create_supervisor_graph, AgentState

logger = structlog.get_logger(__name__)


async def _append_trace(run: Run, entry: dict) -> None:
    """Append a structured entry to the run's trace log."""
    if run.trace is None:
        run.trace = []
    run.trace = run.trace + [
        {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    ]


async def _update_run(run_id: str, **kwargs) -> None:
    """Helper: open a short-lived session and patch the Run row."""
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
    Entry point called by FastAPI BackgroundTasks.
    Executes the multi-agent graph with the ephemeral Docker sandbox.
    """
    logger.info("worker_starting_phase5", run_id=run_id)

    # 1. Load run
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.error("run_not_found", run_id=run_id)
            return
        repo_url = run.repo_url
        issue_text = run.issue_text or (
            f"GitHub issue #{run.issue_number}" if run.issue_number else ""
        )

    sandbox: SandboxManager | None = None
    try:
        await _update_run(run_id, status=RunStatus.CLONING)

        cfg = SandboxConfig()
        sandbox = SandboxManager(repo_url=repo_url, run_id=run_id, config=cfg)
        container_id = await sandbox.start()
        await _update_run(run_id, sandbox_container_id=container_id)

        # Initialize Supervisor Graph & initial state
        supervisor_graph = create_supervisor_graph()

        initial_state: AgentState = {
            "run_id": run_id,
            "repo_url": repo_url,
            "issue_text": issue_text,
            "sandbox": sandbox,
            "file_tree": "",
            "repo_meta": {},
            "relevant_files": [],
            "search_queries": [],
            "search_hits": [],
            "fix_plan": "",
            "target_files": [],
            "tests_to_run": [],
            "iteration": 0,
            "max_iterations": 3,
            "test_feedback": None,
            "test_results": None,
            "all_tests_passed": False,
            "diff": "",
            "review_approved": False,
            "review_notes": "",
            "security_concerns": [],
            "current_status": RunStatus.CLONING,
            "error": None,
        }

        # Stream / run nodes in the supervisor graph
        final_state: dict[str, Any] = {}
        async for output in supervisor_graph.astream(initial_state):
            for node_name, node_update in output.items():
                logger.info("supervisor_node_completed", node=node_name)
                final_state.update(node_update)

                # Persist intermediate status and outputs
                status_to_set = node_update.get("current_status")
                updates: dict[str, Any] = {}
                if status_to_set:
                    updates["status"] = status_to_set
                if "file_tree" in node_update:
                    updates["file_tree"] = node_update["file_tree"]
                if "relevant_files" in node_update:
                    updates["relevant_files"] = node_update["relevant_files"]
                if "fix_plan" in node_update:
                    updates["fix_plan"] = node_update["fix_plan"]
                if "diff" in node_update:
                    updates["diff"] = node_update["diff"]
                if "test_results" in node_update:
                    updates["test_results"] = node_update["test_results"]
                if "review_notes" in node_update:
                    updates["review_notes"] = node_update["review_notes"]

                if updates:
                    await _update_run(run_id, **updates)

                # Record node trace event
                async with AsyncSessionLocal() as session:
                    result = await session.execute(select(Run).where(Run.id == run_id))
                    run_obj = result.scalar_one()
                    await _append_trace(
                        run_obj,
                        {
                            "type": f"supervisor_node_{node_name}",
                            "data": {k: v for k, v in node_update.items() if k != "sandbox"},
                        },
                    )
                    await session.commit()

        # Mark Run as Done
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Run).where(Run.id == run_id))
            run_obj = result.scalar_one()
            run_obj.status = RunStatus.DONE
            run_obj.diff = final_state.get("diff", "")
            run_obj.test_results = final_state.get("test_results")
            run_obj.review_notes = final_state.get("review_notes", "")
            run_obj.updated_at = datetime.now(timezone.utc)
            run_obj.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info(
            "phase5_complete",
            run_id=run_id,
            review_approved=final_state.get("review_approved"),
        )

    except Exception as exc:
        logger.exception("worker_error", run_id=run_id)
        await _update_run(
            run_id,
            status=RunStatus.FAILED,
            error_message=str(exc),
        )
    finally:
        if sandbox is not None:
            try:
                await sandbox.destroy()
            except Exception:
                logger.warning("sandbox_destroy_failed_in_finally", run_id=run_id)


if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.INFO)
    logger.info("worker_standalone_mode_not_yet_implemented")
    sys.exit(0)
