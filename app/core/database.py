import asyncio

from sqlalchemy import text
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


async def warmup(connections: int = 1) -> None:
    """Pre-open DB connections and configure ORM mappers so the first
    incoming request does not pay the cold-start cost."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


    


async def close_db():
    await engine.dispose()
    
class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
