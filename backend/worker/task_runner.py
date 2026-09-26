"""
Worker — Phase 3 task runner.

Pipeline:
  1. Load the Run from the database.
  2. Spin up a SandboxManager → clone → inspect (Phase 2).
  3. Planner LLM call: issue + file_tree → ranked files + search queries + plan.
  4. CodeSearch: grep queries inside sandbox → ranked match set.
  5. Analyzer LLM call: reads top files → concrete fix plan.
  6. Persist all Phase 3 outputs (relevant_files, fix_plan) and set status=DONE.
  7. Destroy sandbox (always, even on error).

Phase 4+: this file will grow to include the code-editor and PR agents.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.database import AsyncSessionLocal
from models.run import Run, RunStatus
from sandbox.config import SandboxConfig
from sandbox.manager import SandboxManager
from agents.planner import Planner
from agents.code_search import CodeSearch
from agents.analyzer import Analyzer

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Main task ─────────────────────────────────────────────────────────────────

async def run_task(run_id: str) -> None:
    """
    Entry point called by FastAPI BackgroundTasks.

    Full Phase 3 pipeline: clone → inspect → plan → search → analyze → persist.
    """
    logger.info("worker_starting", run_id=run_id)

    # ── Load run ─────────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Run).where(Run.id == run_id))
        run = result.scalar_one_or_none()
        if not run:
            logger.error("run_not_found", run_id=run_id)
            return
        repo_url   = run.repo_url
        issue_text = run.issue_text or (
            f"GitHub issue #{run.issue_number}" if run.issue_number else ""
        )

    sandbox: SandboxManager | None = None
    try:
        # ── Phase 2: Clone + Inspect ──────────────────────────────────────────
        await _update_run(run_id, status=RunStatus.CLONING)
        logger.info("cloning_repo", run_id=run_id, repo_url=repo_url)

        cfg     = SandboxConfig()
        sandbox = SandboxManager(repo_url=repo_url, run_id=run_id, config=cfg)
        container_id = await sandbox.start()
        await _update_run(run_id, sandbox_container_id=container_id)

        clone_result = await sandbox.clone_repo()
        if not clone_result.success:
            raise RuntimeError(f"git clone failed:\n{clone_result.stderr}")

        await _update_run(run_id, status=RunStatus.INSPECTING)
        logger.info("inspecting_repo", run_id=run_id)

        repo_meta = await sandbox.get_repo_metadata()
        file_tree = await sandbox.list_files()
        await _update_run(run_id, file_tree=file_tree)

        dep_result = await sandbox.install_dependencies()
        logger.info(
            "dep_install_done",
            run_id=run_id,
            exit_code=dep_result.exit_code,
        )

        _add_inspection_trace_entry = {
            "type": "inspection_complete",
            "data": {
                "total_files":           repo_meta.total_files,
                "primary_language":      repo_meta.primary_language,
                "test_framework":        repo_meta.test_framework,
                "dep_files":             repo_meta.dep_files,
                "test_dirs":             repo_meta.test_dirs,
                "dep_install_exit_code": dep_result.exit_code,
            },
        }

        # ── Phase 3a: Planner ─────────────────────────────────────────────────
        await _update_run(run_id, status=RunStatus.PLANNING)
        logger.info("planner_starting", run_id=run_id)

        planner = Planner()
        planner_result = await planner.plan(
            issue_text=issue_text,
            file_tree=file_tree,
            repo_meta={
                "primary_language": repo_meta.primary_language,
                "test_framework":   repo_meta.test_framework,
                "dep_files":        repo_meta.dep_files,
                "test_dirs":        repo_meta.test_dirs,
            },
        )

        logger.info(
            "planner_complete",
            run_id=run_id,
            files=len(planner_result.relevant_files),
            queries=len(planner_result.search_queries),
        )

        # ── Phase 3b: CodeSearch ──────────────────────────────────────────────
        logger.info("code_search_starting", run_id=run_id)

        searcher = CodeSearch(sandbox)
        search_result = await searcher.search(planner_result.search_queries)

        logger.info(
            "code_search_complete",
            run_id=run_id,
            matches=len(search_result.matches),
            unique_files=len(search_result.unique_files),
        )

        # Serialise search hits for DB + prompt
        search_hits_serialised: list[dict[str, Any]] = [
            {"file": m.file, "line": m.line, "content": m.content, "query": m.query}
            for m in search_result.matches
        ]

        # Merge Planner's relevant_files with CodeSearch unique_files (Planner first)
        merged_files: list[str] = list(planner_result.relevant_files)
        for f in search_result.unique_files:
            if f not in merged_files:
                merged_files.append(f)

        # Persist relevant files early (visible in UI while Analyzer runs)
        await _update_run(run_id, relevant_files=merged_files[:20])

        # ── Phase 3c: Analyzer ────────────────────────────────────────────────
        logger.info("analyzer_starting", run_id=run_id)

        analyzer = Analyzer(sandbox=sandbox)
        analyzer_result = await analyzer.analyze(
            issue_text=issue_text,
            planner_plan=planner_result.plan_text,
            relevant_files=merged_files,
            search_hits=search_hits_serialised,
        )

        logger.info(
            "analyzer_complete",
            run_id=run_id,
            confidence=analyzer_result.confidence,
            files_to_edit=len(analyzer_result.files_to_edit),
        )

        # ── Persist all Phase 3 results ───────────────────────────────────────
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Run).where(Run.id == run_id))
            run_obj = result.scalar_one()

            # Append all trace events
            await _append_trace(run_obj, _add_inspection_trace_entry)
            await _append_trace(run_obj, {
                "type": "planner_complete",
                "data": {
                    "summary":        planner_result.summary,
                    "relevant_files": planner_result.relevant_files,
                    "search_queries": planner_result.search_queries,
                    "input_tokens":   planner_result.usage_input_tokens,
                    "output_tokens":  planner_result.usage_output_tokens,
                },
            })
            await _append_trace(run_obj, {
                "type": "code_search_complete",
                "data": {
                    "queries_run":   search_result.queries_run,
                    "queries_empty": search_result.queries_empty,
                    "total_matches": len(search_result.matches),
                    "unique_files":  search_result.unique_files,
                    "top_hits":      search_hits_serialised[:20],
                },
            })
            await _append_trace(run_obj, {
                "type": "analyzer_complete",
                "data": {
                    "files_read":     analyzer_result.files_read,
                    "files_to_edit":  analyzer_result.files_to_edit,
                    "tests_to_run":   analyzer_result.tests_to_run,
                    "confidence":     analyzer_result.confidence,
                    "input_tokens":   analyzer_result.usage_input_tokens,
                    "output_tokens":  analyzer_result.usage_output_tokens,
                },
            })

            # Final field updates
            run_obj.relevant_files = merged_files[:20]
            run_obj.fix_plan       = analyzer_result.fix_plan
            run_obj.status         = RunStatus.DONE
            run_obj.updated_at     = datetime.now(timezone.utc)
            run_obj.completed_at   = datetime.now(timezone.utc)
            await session.commit()

        logger.info(
            "phase3_complete",
            run_id=run_id,
            language=repo_meta.primary_language,
            relevant_files=len(merged_files),
            confidence=analyzer_result.confidence,
        )

    except Exception as exc:
        logger.exception("worker_error", run_id=run_id)
        await _update_run(
            run_id,
            status=RunStatus.FAILED,
            error_message=str(exc),
        )
    finally:
        # Always destroy the sandbox container
        if sandbox is not None:
            try:
                await sandbox.destroy()
            except Exception:
                logger.warning("sandbox_destroy_failed_in_finally", run_id=run_id)


# ── Standalone entry point ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    logger.info("worker_standalone_mode_not_yet_implemented")
    sys.exit(0)
