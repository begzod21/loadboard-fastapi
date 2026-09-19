import asyncio
import time
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, configure_mappers

from app.core.config import settings

engine = create_async_engine(
    settings.db.async_db_url,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_use_lifo=True,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

# --- Per-request DB instrumentation (consumed by RequestTimingMiddleware) ---
_db_query_count: ContextVar[int] = ContextVar("db_query_count", default=0)
_db_time_ms: ContextVar[float] = ContextVar("db_time_ms", default=0.0)


def _before_execute(conn, clauseelement, multiparams, params, execution_options):
    conn.info["_loadboard_query_start"] = time.perf_counter()


def _after_execute(conn, clauseelement, multiparams, params, execution_options, result):
    start = conn.info.pop("_loadboard_query_start", None)
    if start is None:
        return
    elapsed = (time.perf_counter() - start) * 1000.0
    _db_query_count.set(_db_query_count.get() + 1)
    _db_time_ms.set(_db_time_ms.get() + elapsed)


event.listen(engine.sync_engine, "before_execute", _before_execute)
event.listen(engine.sync_engine, "after_execute", _after_execute)


def reset_db_instrumentation() -> None:
    _db_query_count.set(0)
    _db_time_ms.set(0.0)


def get_db_instrumentation() -> tuple[int, float]:
    return _db_query_count.get(), _db_time_ms.get()


async def warmup(connections: int = 1) -> None:
    """Pre-open DB connections and configure ORM mappers so the first
    incoming request does not pay the cold-start cost."""
    configure_mappers()

    async def _open() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    await asyncio.gather(*(_open() for _ in range(max(1, connections))))


async def close_db():
    await engine.dispose()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
