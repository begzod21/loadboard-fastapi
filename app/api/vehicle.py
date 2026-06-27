from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import CurrentUser, get_current_user
from ..core.dependencies import get_tenant_db
from ..filters.vehicle import VehicleFilter, vehicle_filter_params
from ..schemas.vehicle import PaginatedVehicles
from ..services.vehicle import VehicleListParams, VehicleListService

router = APIRouter(prefix="/app/api", tags=["vehicle"])


@router.get("/owner/vehicle/list/", response_model=PaginatedVehicles)
async def list_vehicles(
    request: Request,
    latitude: float | None = Query(default=None, description="Latitude of the location"),
    longitude: float | None = Query(default=None, description="Longitude of the location"),
    radius: float | None = Query(default=None, description="Radius in miles"),
    address: str | None = Query(default=None, description="Address to search nearby vehicles"),
    load_id: int | None = Query(default=None, description="ID of the load for location lookup"),
    bid_id: int | None = Query(default=None, description="ID of the bid for location lookup"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    filters: VehicleFilter = Depends(vehicle_filter_params),
    session: AsyncSession = Depends(get_tenant_db),
    user: CurrentUser = Depends(get_current_user),
) -> PaginatedVehicles:
    tenant_cargo_distance = request.state.tenant.cargo_distance
    resolved_radius = (
        radius
        if radius is not None
        else (tenant_cargo_distance if tenant_cargo_distance is not None else -1)
    )

    service = VehicleListService(
        session,
        user,
        mapbox_token=request.state.tenant.mapbox_token,
    )
    params = VehicleListParams(
        latitude=latitude,
        longitude=longitude,
        address=address,
        radius=resolved_radius,
        load_id=load_id,
        bid_id=bid_id,
        page=page,
        page_size=page_size,
    )
    try:
        count, results = await service.list(params, filters)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    base_url = request.url

    has_next = page * page_size < count
    next_url = str(base_url.include_query_params(page=page + 1)) if has_next else None
    prev_url = str(base_url.include_query_params(page=page - 1)) if page > 1 else None

    return PaginatedVehicles(
        count=count,
        next=next_url,
        previous=prev_url,
        results=results,
    )
