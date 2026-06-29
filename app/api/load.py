from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import CurrentUser, get_current_user
from ..core.dependencies import get_tenant_db
from ..filters.load import LoadFilter, load_filter_params
from ..schemas.load import PaginatedLoads, LoadDetailSchema
from ..services.load import LoadListParams, LoadListService, LoadDetailService

router = APIRouter(prefix="/api/v1/load", tags=["load"])


@router.get("/list/", response_model=PaginatedLoads)
async def list_loads(
    request: Request,
    cargo_distance: float | None = Query(default=None, description="Override tenant cargo_distance"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    filters: LoadFilter = Depends(load_filter_params),
    session: AsyncSession = Depends(get_tenant_db),
    user: CurrentUser = Depends(get_current_user),
) -> PaginatedLoads:
    tenant_cargo_distance = request.state.tenant.cargo_distance
    resolved_cargo_distance = (
        cargo_distance
        if cargo_distance is not None
        else (tenant_cargo_distance if tenant_cargo_distance is not None else -1)
    )

    service = LoadListService(session, user)
    params = LoadListParams(
        cargo_distance=resolved_cargo_distance,
        page=page,
        page_size=page_size,
    )
    count, results = await service.list(params, filters)

    base_url = request.url
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto:
        base_url = base_url.replace(scheme=forwarded_proto.split(",")[0].strip())

    has_next = page * page_size < count
    next_url = str(base_url.include_query_params(page=page + 1)) if has_next else None
    prev_url = str(base_url.include_query_params(page=page - 1)) if page > 1 else None

    return PaginatedLoads(
        count=count,
        next=next_url,
        previous=prev_url,
        results=results,
    )


# @router.get("/{load_id}/", response_model=LoadDetailSchema)
# async def retrieve_load(
#     load_id: int,
#     session: AsyncSession = Depends(get_tenant_db),
#     user: CurrentUser = Depends(get_current_user),
# ) -> LoadDetailSchema:
#     service = LoadDetailService(session, user)
#     load = await service.get(load_id)
#     if load is None:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
#     return load

