from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.schemas.company import TenantCompanyOut

from core.database import engine

async def get_tenant(request: Request) -> TenantCompanyOut:
    if not hasattr(request.state, "tenant"):
        raise Exception("Tenant not set in request state. Ensure tenant_middleware is applied.")
    return request.state.tenant

async def get_tenant_db(
        tenant: TenantCompanyOut = Depends(get_tenant),
) -> AsyncSession: # type: ignore
    print("Before:", engine.sync_engine.pool.status())
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text(f"SET search_path TO {tenant.schema_name}, public"))
            yield session

    print("After:", engine.sync_engine.pool.status())