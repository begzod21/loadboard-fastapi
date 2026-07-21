from sqlalchemy import text, select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import AsyncSessionLocal
from app.models.load import load_is_read_users


async def mark_load_read(
    load_id: int,
    user_id: int,
    tenant_schema: str,
) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(f"SET search_path TO {tenant_schema}, public")
            )

            stmt = (
                insert(load_is_read_users)
                .values(
                    load_id=load_id,
                    user_id=user_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["load_id", "user_id"]
                )
            )

            await session.execute(stmt)