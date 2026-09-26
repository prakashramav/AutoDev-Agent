# AutoDev-Agent 🤖

An autonomous AI software engineering agent that takes a GitHub issue and — without human intervention — understands the issue, inspects the repository, locates relevant files, plans a fix, modifies code in an isolated sandbox, runs tests, iterates on failures, runs a security review, and opens a Pull Request.

## Architecture

```
User (issue link/text)
  └─► FastAPI API
        └─► Supervisor Agent (LangGraph)
              ├─► Code Agent
              ├─► Test Agent
              └─► Review Agent
                    └─► Sandbox (Docker — ephemeral, resource-limited)
                          └─► GitHub API (branch, commit, PR)
```

## Core Loop

```
Understand Issue → Inspect Repo → Find Relevant Files → Analyze Code
  → Create Plan → Modify Code (sandbox) → Run Tests → Detect Failure
  → Fix → Run Tests Again → Security Review → Create Branch → Commit → PR
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend API | FastAPI (Python 3.12) |
| Agent orchestration | LangGraph |
| LLM | Claude (Anthropic) |
| Sandbox | Docker (ephemeral containers, resource-limited) |
| GitHub | REST/GraphQL API + git CLI inside sandbox |
| Test execution | pytest (language-agnostic by design) |
| Database | PostgreSQL (task/run state, audit trail) |
| Queue | Redis (job queue + short-term agent memory) |
| Deployment | Docker Compose (local), Railway/Render/AWS |

## Setup

### Prerequisites
- Docker & Docker Compose
- Python 3.12+
- GitHub Personal Access Token
- Anthropic API Key

### Quick Start

```bash
# 1. Clone and enter project
git clone <repo-url>
cd autodev-agent

# 2. Configure environment
cp .env.example .env
# Fill in GITHUB_TOKEN, ANTHROPIC_API_KEY, and DB credentials

# 3. Start all services
docker-compose up --build

# 4. Submit a task
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/owner/repo", "issue_number": 143}'
```

## Build Phases

- [x] **Phase 1**: Core scaffold — API, sandbox clone, Postgres run tracking
- [x] **Phase 2**: Sandbox execution primitives (`SandboxConfig`, `TestResult`, `install_dependencies`, `get_repo_metadata`, hardened `write_file`)
- [ ] **Phase 3**: Understand Issue → Plan (Planner LLM + CodeSearch)
- [ ] **Phase 4**: Modify → Test → Iterate loop
- [ ] **Phase 5**: Multi-agent supervisor (LangGraph)
- [ ] **Phase 6**: GitHub integration (branch, commit, PR)
- [ ] **Phase 7**: Observability (trace persistence, frontend)

### Network Isolation (Phase 2)

Sandbox containers run on an isolated Docker bridge network (`sandbox_net`).
To enforce egress allowlisting (only GitHub / pip / npm / Go / Cargo registries):

```bash
# Run once on the Docker host (Linux only — requires root + iptables)
sudo bash scripts/setup_sandbox_network.sh
```

On macOS/Windows Docker Desktop, the host network stack is inside a Linux VM —
the script still works but must be run inside the VM or via Docker Desktop settings.

## Safety Constraints

- The **host process NEVER executes cloned or generated code** — everything runs inside the ephemeral sandbox container, which is destroyed after the run.
- Every external side effect (branch creation, commit, PR) is logged and in **dev mode requires explicit confirmation** before hitting real GitHub.
- Sandbox containers have strict resource limits (CPU, memory, network egress restricted to GitHub/pip/npm only).

## Environment Variables

See [`.env.example`](.env.example) for all required variables.

## Demo

> **Coming in Phase 7** — paste an issue → watch the agent trace → see the resulting PR.

## License

MIT
