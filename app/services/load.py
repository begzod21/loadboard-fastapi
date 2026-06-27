from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.security import CurrentUser
from ..filters.load import LoadFilter
from ..models.load import (
    Bid,
    DriverBid,
    Load,
    load_is_read_users,
    load_vehicle_teams,
)
from ..models.vehicle import Vehicle
from ..schemas.load import LoadListSchema


@dataclass
class LoadListParams:
    cargo_distance: float | None = None
    page: int = 1
    page_size: int = 20


class LoadListService:
    def __init__(self, session: AsyncSession, user: CurrentUser) -> None:
        self.session = session
        self.user = user
        self.team_ids = user.team_ids

    async def list(
        self, params: LoadListParams, filters: LoadFilter
    ) -> tuple[int, list[LoadListSchema]]:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)
        cargo_distance = (
            params.cargo_distance
            if params.cargo_distance is not None
            else -1
        )

        clauses = [Load.is_active.is_(True), Load.is_deleted.is_(False)]
        if cargo_distance != -1:
            clauses.append(Load.nearest_vehicles_count > 0)

        vehicle_scope = [Vehicle.status == 1, Vehicle.registration_status == 4]
        if self.team_ids:
            vehicle_scope.append(Vehicle.team_id.in_(self.team_ids))

        if self.team_ids:
            has_team = exists(
                select(load_vehicle_teams.c.id).where(
                    load_vehicle_teams.c.load_id == Load.id,
                    load_vehicle_teams.c.team_id.in_(self.team_ids),
                )
            )
            clauses.append(
                or_(
                    Load.has_driver_in_all_teams.is_(True),
                    and_(Load.has_driver_in_all_teams.is_(False), has_team),
                )
            )

        is_bid_col = exists(
            select(Bid.id)
            .join(Vehicle, Vehicle.id == Bid.vehicle_id)
            .where(Bid.load_id == Load.id, Bid.created_at >= cutoff, *vehicle_scope)
        )
        is_driver_bid_col = exists(
            select(DriverBid.id)
            .join(Vehicle, Vehicle.id == DriverBid.vehicle_id)
            .where(
                DriverBid.load_id == Load.id,
                DriverBid.dispatch_bid_date.is_(None),
                DriverBid.created_at >= cutoff,
                *vehicle_scope,
            )
        )
        if self.user.user_id is not None:
            is_read_col = exists(
                select(load_is_read_users.c.id).where(
                    load_is_read_users.c.load_id == Load.id,
                    load_is_read_users.c.user_id == self.user.user_id,
                )
            )
        else:
            is_read_col = None

        # django-filter clauses
        clauses.extend(filters.conditions())
        where = and_(*clauses)

        count = await self.session.scalar(
            select(func.count()).select_from(Load).where(where)
        )

        cols = [Load.id, is_bid_col.label("is_bid"), is_driver_bid_col.label("is_driver_bid")]
        if is_read_col is not None:
            cols.append(is_read_col.label("is_read"))

        ann_stmt = (
            select(*cols)
            .where(where)
            .order_by(Load.received_date.desc())
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
        rows = (await self.session.execute(ann_stmt)).mappings().all()
        if not rows:
            return int(count or 0), []

        load_ids = [row["id"] for row in rows]
        loads = (
            await self.session.scalars(
                select(Load)
                .where(Load.id.in_(load_ids))
                .options(selectinload(Load.vehicle_teams))
            )
        ).unique().all()
        by_id = {load.id: load for load in loads}

        results: list[LoadListSchema] = []
        for row in rows:
            load = by_id.get(row["id"])
            if load is None:
                continue
            results.append(
                LoadListSchema.from_load(
                    load,
                    is_bid=row.get("is_bid"),
                    is_driver_bid=row.get("is_driver_bid"),
                    is_read=row.get("is_read", False),
                    radius=cargo_distance,
                )
            )
        return int(count or 0), results
