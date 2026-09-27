"""Async SQLAlchemy engine and session factory."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from pathlib import Path

from core.config import settings

_db_url = settings.DATABASE_URL
_is_sqlite = _db_url.startswith("sqlite")

connect_args = {}

if _is_sqlite:
    if "./" in _db_url:
        # Anchor SQLite database to backend directory so working directory doesn't change database location
        _backend_dir = Path(__file__).resolve().parent.parent
        _db_file = _db_url.split("./")[-1]
        _abs_path = (_backend_dir / _db_file).resolve().as_posix()
        _db_url = f"sqlite+aiosqlite:///{_abs_path}"
    connect_args["check_same_thread"] = False
else:
    # Normalize postgres / postgresql schemes to asyncpg
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _db_url.startswith("postgresql://") and not _db_url.startswith("postgresql+asyncpg://"):
        _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # Clean libpq query parameters incompatible with asyncpg (e.g., sslmode, channel_binding)
    if "neon.tech" in _db_url or "sslmode=" in _db_url or "ssl=require" in _db_url:
        connect_args["ssl"] = True
        if "?" in _db_url:
            _db_url = _db_url.split("?")[0]

engine = create_async_engine(
    _db_url,
    echo=(settings.APP_ENV == "development"),
    # SQLite doesn't support connection pooling the same way as Postgres
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}),
    connect_args=connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
