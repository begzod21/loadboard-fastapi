import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, configure_mappers

from app.core.config import settings

engine = create_async_engine(
    settings.db.async_db_url,
    pool_size=100,
    max_overflow=60,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)


async def warmup(connections: int = 10) -> None:
    """Pre-open DB connections and configure ORM mappers so the first
    incoming request does not pay the cold-start cost."""
    configure_mappers()

    async def _open() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    await asyncio.gather(*(_open() for _ in range(connections)))


async def close_db():
    await engine.dispose()
    
class Base(DeclarativeBase):
    """Declarative base for all ORM models."""