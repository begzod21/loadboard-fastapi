import logging
import re

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import redis_client
from app.schemas.company import TenantCompanyOut

logger = logging.getLogger(__name__)

# schema_name is interpolated into SET search_path (bind params are not
# allowed for identifiers) — keep it strictly alphanumeric to block SQLi.
_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CACHE_PREFIX = "tenant:domain:"


async def _cached_tenant(domain: str) -> TenantCompanyOut | None:
    if settings.TENANT_CACHE_TTL <= 0:
        return None
    try:
        raw = await redis_client.get(f"{_CACHE_PREFIX}{domain}")
    except Exception as exc:  # Redis down → transparent DB fallback
        logger.warning("tenant cache read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return TenantCompanyOut.model_validate_json(raw)
    except ValueError:
        return None


async def _store_tenant(domain: str, tenant: TenantCompanyOut) -> None:
    if settings.TENANT_CACHE_TTL <= 0:
        return
    try:
        await redis_client.set(
            f"{_CACHE_PREFIX}{domain}",
            tenant.model_dump_json(),
            ex=settings.TENANT_CACHE_TTL,
        )
    except Exception as exc:
        logger.warning("tenant cache write failed: %s", exc)


async def get_tenant_db(
    request: Request,
) -> AsyncSession:
    domain = request.url.hostname or "localhost"
    tenant = await _cached_tenant(domain)

    async with AsyncSessionLocal() as session:
        if tenant is None:
            row = (
                await session.execute(
                    text(
                        f"""
                        SELECT id, schema_name, domain_url, cargo_distance, mapbox_token
                        FROM {settings.tenant_table}
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

            tenant = TenantCompanyOut(
                id=row.id,
                schema_name=row.schema_name,
                domain_url=row.domain_url,
                cargo_distance=row.cargo_distance,
                mapbox_token=row.mapbox_token,
            )
            await _store_tenant(domain, tenant)

        if not _SCHEMA_NAME_RE.match(tenant.schema_name):
            raise HTTPException(400, f"Invalid schema name for {domain}")

        request.state.tenant = tenant

        await session.execute(
            text(f'SET search_path TO "{tenant.schema_name}", public')
        )

        yield session
