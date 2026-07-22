import sys
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.schemas.company import TenantCompanyOut


async def get_tenant_db(
    request: Request,
) -> AsyncSession:
    domain = request.headers.get("host", "localhost")

    async with AsyncSessionLocal() as session:
        sid = id(session)
        print(f"[session {sid}] open", flush=True)
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, schema_name, domain_url, cargo_distance, mapbox_token
                        FROM company_company
                        WHERE domain_url = :domain
                        """
                    ),
                    {"domain": domain},
                )
            ).fetchone()

            if row is None:
                raise HTTPException(404, f"Company not found for domain: {domain}")
            if not row.schema_name:
                raise HTTPException(400, f"Schema name not defined for {domain}")

            request.state.tenant = TenantCompanyOut(
                id=row.id,
                schema_name=row.schema_name,
                domain_url=row.domain_url,
                cargo_distance=row.cargo_distance,
                mapbox_token=row.mapbox_token,
            )

            await session.execute(
                text(f'SET search_path TO "{row.schema_name}", public')
            )

            yield session
        finally:
            print(f"[session {sid}] close", flush=True)
