from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import and_, exists, func, or_, select, insert, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import CurrentUser
from ..filters.load import LoadFilter
from ..models.load import (
    Bid,
    DriverBid,
    Load,
    load_is_read_users,
    load_vehicle_teams,
)
from ..models.vehicle import Driver, Vehicle
from ..schemas.load import LoadListSchema, BidInfoSchema, LoadDetailSchema
from ..schemas.company import TenantCompanyOut


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
        if not count:
            return 0, []

        cols = [Load, is_bid_col.label("is_bid"), is_driver_bid_col.label("is_driver_bid")]
        if is_read_col is not None:
            cols.append(is_read_col.label("is_read"))

        stmt = (
            select(*cols)
            .where(where)
            .order_by(Load.received_date.desc())
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
        rows = (await self.session.execute(stmt)).unique().all()

        results: list[LoadListSchema] = [
            LoadListSchema.from_load(
                row[0],
                is_bid=row.is_bid,
                is_driver_bid=row.is_driver_bid,
                is_read=getattr(row, "is_read", False),
                radius=cargo_distance,
            )
            for row in rows
        ]
        return int(count), results

class LoadDetailService:
    def __init__(self, session: AsyncSession, user: CurrentUser, tenant: TenantCompanyOut | None) -> None:
        self.session = session
        self.user = user
        self.tenant: TenantCompanyOut | None = tenant

    async def get(self, load_id: int) -> LoadDetailSchema | None:
        load = await self.session.scalar(
            select(Load)
            .where(Load.id == load_id, Load.is_deleted.is_(False))
            .options(selectinload(Load.points))
        )
        if load is None:
            return None

        perms = self.user.permissions
        if self.user.is_superuser or "VIEW_ALL_BIDS_WITH_PRICES" in perms:
            view_mode = "with_prices"
        elif "VIEW_ALL_BIDS_WITHOUT_PRICES" in perms:
            view_mode = "without_prices"
        elif "VIEW_OWN_BIDS" in perms:
            view_mode = "own"
        else:
            view_mode = None

        bid_info = None
        if view_mode is not None:
            stmt = (
                select(
                    Bid.vehicle_id,
                    Bid.created_at,
                    Bid.driver_price,
                    Bid.broker_price,
                    Bid.dispatcher_id,
                    Vehicle.team_id,
                    Vehicle.object_id,
                    Vehicle.driver_id,
                )
                .select_from(Bid)
                .join(Vehicle, Vehicle.id == Bid.vehicle_id, isouter=True)
                .where(Bid.load_id == load.id)
                .order_by(Bid.id.desc())
            )
            if view_mode == "own":
                stmt = stmt.where(Bid.dispatcher_id == self.user.user_id)

            rows = (await self.session.execute(stmt)).mappings().all()

            result = []
            for row in rows:
                vehicle_team = row.get("team_id")
                if vehicle_team and self.user.team_ids and vehicle_team not in self.user.team_ids:
                    continue

                show_prices = (
                    view_mode == "with_prices"
                    or view_mode == "own"
                    or (view_mode == "without_prices" and row.get("dispatcher_id") == self.user.user_id)
                )

                dispatcher_name = None
                driver_name = None

                if row.get("dispatcher_id") is not None:
                    user_row = await self.session.execute(
                        text(
                            """
                            SELECT first_name, last_name
                            FROM user_user
                            WHERE id = :user_id
                            """
                        ),
                        {"user_id": row.get("dispatcher_id")},
                    )
                    user_data = user_row.first()
                    if user_data is not None:
                        first_name = user_data.first_name or ""
                        last_name = user_data.last_name or ""
                        dispatcher_name = " ".join(part for part in [first_name, last_name] if part).strip() or None

                if row.get("driver_id") is not None:
                    driver = await self.session.scalar(select(Driver).where(Driver.id == row.get("driver_id")))
                    if driver is not None:
                        driver_name = driver.full_name

                result.append(
                    BidInfoSchema(
                        vehicle_id=row.get("object_id") or row.get("vehicle_id"),
                        created_at=row.get("created_at"),
                        dispatcher_name=dispatcher_name,
                        driver_name=driver_name,
                        driver_price=(row.get("driver_price") or 0) if show_prices else 0,
                        broker_price=(row.get("broker_price") or 0) if show_prices else 0,
                    )
                )

            bid_info = result or None

        await self._mark_read(load.id)

        return LoadDetailSchema.from_load(load, bid_info=bid_info, tenant_data=self.tenant)

    async def _mark_read(self, load_id: int) -> None:
        if self.user.user_id is None:
            return
        already = await self.session.scalar(
            select(load_is_read_users.c.id).where(
                load_is_read_users.c.load_id == load_id,
                load_is_read_users.c.user_id == self.user.user_id,
            )
        )
        if already is not None:
            return
        await self.session.execute(
            insert(load_is_read_users).values(
                load_id=load_id, user_id=self.user.user_id
            )
        )
        await self.session.commit()

