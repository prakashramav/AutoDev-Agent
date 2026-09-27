"""
Multi-Agent Supervisor (LangGraph) — Phase 5.

Orchestrates the autonomous software engineering pipeline via a directed state graph:
  [Start]
     │
     ▼
  1. Inspect & Setup (Clone, Dependencies, Repo Metadata)
     │
     ▼
  2. Plan & Locate (Planner LLM + CodeSearch)
     │
     ▼
  3. Analyze (Analyzer LLM)
     │
     ▼
  4. Modify (Code Agent) ◄──────────────┐
     │                                  │ (Iterate if tests fail)
     ▼                                  │
  5. Test (Test Agent) ─────────────────┘
     │ (if all passed or max iterations reached)
     ▼
  6. Review (Review Agent — Security & Quality pass)
     │
     ▼
  [End]
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict, cast
import structlog
from langgraph.graph import StateGraph, END

from models.run import RunStatus
from sandbox.manager import SandboxManager
from agents.planner import Planner
from agents.code_search import CodeSearch
from agents.analyzer import Analyzer
from agents.coder import CodeAgent
from agents.tester import TestAgent
from agents.reviewer import ReviewAgent

logger = structlog.get_logger(__name__)


class AgentState(TypedDict):
    """Shared state dictionary passed across LangGraph nodes."""
    run_id: str
    repo_url: str
    issue_text: str
    sandbox: SandboxManager

    # Inspection
    file_tree: str
    repo_meta: dict[str, Any]

    # Planning & Search
    relevant_files: list[str]
    search_queries: list[str]
    search_hits: list[dict[str, Any]]

    # Analyzer
    fix_plan: str
    target_files: list[str]
    tests_to_run: list[str]

    # Modify & Test Loop
    iteration: int
    max_iterations: int
    test_feedback: str | None
    test_results: dict[str, Any] | None
    all_tests_passed: bool
    diff: str

    # Review
    review_approved: bool
    review_notes: str
    security_concerns: list[str]

    # PR
    pr_url: str | None
    pr_branch: str | None

    # Lifecycle & status tracking
    current_status: RunStatus
    error: str | None


# ── Node Implementations ───────────────────────────────────────────────────────

async def inspect_node(state: AgentState) -> dict[str, Any]:
    """Clone repository, list files, and inspect metadata."""
    sandbox = state["sandbox"]
    logger.info("supervisor_inspect_node_start", run_id=state["run_id"])

    clone_res = await sandbox.clone_repo()
    if not clone_res.success:
        raise RuntimeError(f"git clone failed: {clone_res.stderr}")

    repo_meta = await sandbox.get_repo_metadata()
    file_tree = await sandbox.list_files()
    dep_res = await sandbox.install_dependencies()

    return {
        "file_tree": file_tree,
        "repo_meta": {
            "primary_language": repo_meta.primary_language,
            "test_framework": repo_meta.test_framework,
            "dep_files": repo_meta.dep_files,
            "test_dirs": repo_meta.test_dirs,
            "dep_install_exit_code": dep_res.exit_code,
        },
        "current_status": RunStatus.INSPECTING,
    }


async def plan_node(state: AgentState) -> dict[str, Any]:
    """Run Planner agent and CodeSearch to find relevant files."""
    logger.info("supervisor_plan_node_start", run_id=state["run_id"])
    planner = Planner()
    plan_res = await planner.plan(
        issue_text=state["issue_text"],
        file_tree=state["file_tree"],
        repo_meta=state["repo_meta"],
    )

    searcher = CodeSearch(state["sandbox"])
    search_res = await searcher.search(plan_res.search_queries)

    hits_serialised = [
        {"file": m.file, "line": m.line, "content": m.content, "query": m.query}
        for m in search_res.matches
    ]

    merged_files = list(plan_res.relevant_files)
    for f in search_res.unique_files:
        if f not in merged_files:
            merged_files.append(f)

    return {
        "relevant_files": merged_files[:20],
        "search_queries": plan_res.search_queries,
        "search_hits": hits_serialised,
        "current_status": RunStatus.PLANNING,
    }


async def analyze_node(state: AgentState) -> dict[str, Any]:
    """Run Analyzer agent to produce concrete fix plan."""
    logger.info("supervisor_analyze_node_start", run_id=state["run_id"])
    analyzer = Analyzer(sandbox=state["sandbox"])
    analysis = await analyzer.analyze(
        issue_text=state["issue_text"],
        planner_plan="Plan for resolving the issue",
        relevant_files=state["relevant_files"],
        search_hits=state["search_hits"],
    )

    targets = analysis.files_to_edit or state["relevant_files"][:3]

    return {
        "fix_plan": analysis.fix_plan,
        "target_files": targets,
        "tests_to_run": analysis.tests_to_run,
        "current_status": RunStatus.PLANNING,
    }


async def modify_node(state: AgentState) -> dict[str, Any]:
    """Code Agent generates and applies edits in sandbox."""
    iteration = state["iteration"] + 1
    logger.info("supervisor_modify_node_start", iteration=iteration)

    coder = CodeAgent(sandbox=state["sandbox"])
    coder_res = await coder.generate_and_apply_edits(
        issue_text=state["issue_text"],
        fix_plan=state["fix_plan"],
        target_files=state["target_files"],
        test_feedback=state["test_feedback"],
        iteration=iteration,
    )

    return {
        "iteration": iteration,
        "current_status": RunStatus.MODIFYING,
    }


async def test_node(state: AgentState) -> dict[str, Any]:
    """Test Agent runs tests in sandbox and formats feedback."""
    logger.info("supervisor_test_node_start", iteration=state["iteration"])
    tester = TestAgent(sandbox=state["sandbox"])
    outcome = await tester.execute_tests(
        specific_tests=state["tests_to_run"] or None
    )

    test_dict = (
        outcome.test_result.to_dict()
        if outcome.test_result
        else {"summary": outcome.summary, "passed": outcome.passed}
    )

    diff = await state["sandbox"].get_diff()

    return {
        "all_tests_passed": outcome.passed,
        "test_results": test_dict,
        "test_feedback": outcome.failure_feedback,
        "diff": diff,
        "current_status": RunStatus.TESTING,
    }


async def review_node(state: AgentState) -> dict[str, Any]:
    """Review Agent evaluates the final diff for security and code quality."""
    logger.info("supervisor_review_node_start")
    reviewer = ReviewAgent()
    review = await reviewer.review_diff(
        issue_text=state["issue_text"],
        diff=state["diff"],
        test_summary=state["test_results"].get("summary") if state["test_results"] else None,
    )

    return {
        "review_approved": review.approved,
        "review_notes": review.full_review_markdown,
        "security_concerns": review.security_concerns,
        "current_status": RunStatus.REVIEWING,
    }


async def create_pr_node(state: AgentState) -> dict[str, Any]:
    """Commit changes to a new branch and open a GitHub Pull Request."""
    logger.info("supervisor_create_pr_node_start")
    from core.github import GitHubService
    from core.config import settings

    gh = GitHubService()
    run_id = state["run_id"]
    branch_name = f"autodev/fix-{run_id[:8]}"

    # Commit changes inside sandbox
    commit_msg = f"fix: AutoDev resolution for issue\n\nRun ID: {run_id}"
    await state["sandbox"].commit_changes(
        branch_name=branch_name,
        commit_message=commit_msg,
    )

    # Push branch if enabled or when credentials are provided
    if settings.AUTO_PUSH_TO_GITHUB and settings.GITHUB_TOKEN:
        try:
            await state["sandbox"].push_branch(
                branch_name=branch_name,
                github_token=settings.GITHUB_TOKEN,
            )
        except Exception as exc:
            logger.warning("push_branch_failed", exc=str(exc))

    # Create Pull Request
    pr_title = f"fix: {state['issue_text'].splitlines()[0][:70]}"
    pr_body = (
        f"## 🤖 AutoDev Agent Pull Request\n\n"
        f"### Issue\n{state['issue_text']}\n\n"
        f"### Fix Plan\n{state['fix_plan']}\n\n"
        f"### Security Review\n{state['review_notes']}\n\n"
        f"---\n*Generated autonomously by AutoDev-Agent.*"
    )

    try:
        pr_res = await gh.create_pull_request(
            repo_url=state["repo_url"],
            branch_name=branch_name,
            title=pr_title,
            body=pr_body,
        )
        pr_url = pr_res.pr_url
    except Exception as exc:
        logger.warning("github_pr_creation_failed_fallback", exc=str(exc))
        from core.github import parse_github_repo_url
        try:
            repo_id = parse_github_repo_url(state["repo_url"])
            pr_url = f"https://github.com/{repo_id.full_name}/compare/{branch_name}?expand=1"
        except Exception:
            pr_url = f"{state['repo_url']}/tree/{branch_name}"

    return {
        "pr_url": pr_url,
        "pr_branch": branch_name,
        "current_status": RunStatus.CREATING_PR,
    }


# ── Conditional Routing ────────────────────────────────────────────────────────

def should_continue_testing(state: AgentState) -> str:
    """Decide whether to iterate on code modification or proceed to review."""
    if state["all_tests_passed"]:
        logger.info("supervisor_tests_passed_routing_to_review")
        return "review"

    if state["iteration"] >= state["max_iterations"]:
        logger.warning(
            "supervisor_max_iterations_reached_routing_to_review",
            iterations=state["iteration"],
        )
        return "review"

    logger.info("supervisor_tests_failed_routing_to_modify", iteration=state["iteration"])
    return "modify"


def should_create_pr(state: AgentState) -> str:
    """Only open PR if diff was generated and review is approved."""
    if state.get("review_approved") and state.get("diff"):
        return "create_pr"
    return "end"


# ── Graph Builder ─────────────────────────────────────────────────────────────

def create_supervisor_graph():
    """Build and compile the LangGraph multi-agent supervisor."""
    workflow = StateGraph(AgentState)

    # Add Nodes
    workflow.add_node("inspect", inspect_node)
    workflow.add_node("plan", plan_node)
    workflow.add_node("analyze", analyze_node)
    workflow.add_node("modify", modify_node)
    workflow.add_node("test", test_node)
    workflow.add_node("review", review_node)
    workflow.add_node("create_pr", create_pr_node)

    # Set Entry Point
    workflow.set_entry_point("inspect")

    # Add Edges
    workflow.add_edge("inspect", "plan")
    workflow.add_edge("plan", "analyze")
    workflow.add_edge("analyze", "modify")
    workflow.add_edge("modify", "test")

    # Conditional Branching on Test Results
    workflow.add_conditional_edges(
        "test",
        should_continue_testing,
        {
            "modify": "modify",
            "review": "review",
        },
    )

    # Branch after review
    workflow.add_conditional_edges(
        "review",
        should_create_pr,
        {
            "create_pr": "create_pr",
            "end": END,
        },
    )

    workflow.add_edge("create_pr", END)

    return workflow.compile()
