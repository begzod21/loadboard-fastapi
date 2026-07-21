from fastapi import Request, HTTPException
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.schemas.company import TenantCompanyOut

async def tenant_middleware(request: Request, call_next):
    domain = request.headers.get("host", "localhost")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, schema_name, domain_url, cargo_distance, mapbox_token, bid_message, mc_number
                FROM company_company
                WHERE domain_url = :domain
            """),
            {"domain": domain},
        )
        row = result.fetchone()
    
    if not row:
        raise HTTPException(404, f"Company not found for domain: {domain}")

    if not row.schema_name:
        raise HTTPException(400, f"Schema name not defined for {domain}")
    
    request.state.tenant = TenantCompanyOut(
        id=row.id,
        schema_name=row.schema_name,
        domain_url=row.domain_url,
        cargo_distance=row.cargo_distance,
        mapbox_token=row.mapbox_token
    )

    return await call_next(request)