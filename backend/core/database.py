"""Async SQLAlchemy engine and session factory."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from pathlib import Path

from core.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
_db_url = settings.DATABASE_URL

if _is_sqlite and "./" in _db_url:
    # Anchor SQLite database to backend directory so working directory doesn't change database location
    _backend_dir = Path(__file__).resolve().parent.parent
    _db_file = _db_url.split("./")[-1]
    _abs_path = (_backend_dir / _db_file).resolve().as_posix()
    _db_url = f"sqlite+aiosqlite:///{_abs_path}"

engine = create_async_engine(
    _db_url,
    echo=(settings.APP_ENV == "development"),
    # SQLite doesn't support connection pooling the same way as Postgres
    **({} if _is_sqlite else {"pool_size": 10, "max_overflow": 20}),
    connect_args={"check_same_thread": False} if _is_sqlite else {},
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
