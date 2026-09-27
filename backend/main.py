"""
AutoDev-Agent — FastAPI application entry point. Connected to database.
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import tasks
from core.config import settings
from core.database import engine, Base

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up and shut-down lifecycle."""
    logger.info("starting_up", env=settings.APP_ENV)
    # Create all tables on startup (Alembic handles real migrations;
    # this is a safety-net for fresh dev containers)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database_tables_ready")
    yield
    logger.info("shutting_down")
    await engine.dispose()


app = FastAPI(
    title="AutoDev-Agent API",
    description=(
        "AI-powered autonomous software engineering agent. "
        "Submits a GitHub issue and gets back a pull request."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ─── CORS ──────────────────────────────────────────────────────
# Full permissive CORS for local dev, Render backend, Vercel deployments, and custom frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "https://autodev-agent.onrender.com",
    ],
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ─── Routers ───────────────────────────────────────────────────
app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])


# ─── Health check ──────────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/", tags=["meta"])
async def root():
    return {
        "service": "AutoDev-Agent",
        "docs": "/docs",
        "health": "/health",
    }
