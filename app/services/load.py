from __future__ import annotations
import asyncio
import datetime
import logging
import math
import re
from dataclasses import dataclass

from sqlalchemy import (
    Integer,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload, noload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from geoalchemy2 import Geography

from ..core.security import CurrentUser
from ..filters.load import LoadFilter
from ..models.load import (
    Bid,
    DriverBid,
    Load,
    load_is_read_users,
    load_pinned_users,
    load_vehicle_teams,
)
from ..models.vehicle import Driver, Team, Vehicle, VehicleType
from ..schemas.load import (
    BidInfoSchema,
    LoadDetailInfoSchema,
    LoadDetailSchema,
    LoadListSchema,
)
from ..schemas.company import TenantCompanyOut
from .notify import SenderToWebSocket

logger = logging.getLogger(__name__)

# strong refs to in-flight fire-and-forget tasks (prevents GC of running tasks)
_background_tasks: set[asyncio.Task] = set()


def _is_truthy(value: object | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "t", "yes", "y", "1")


TYPE_SYNONYMS: dict[str, set[str]] = {
    "V": {"V", "VAN", "DRY VAN"},
    "VAN": {"V", "VAN", "DRY VAN"},
    "DRY VAN": {"V", "VAN", "DRY VAN"},
    "R": {"R", "REEFER", "REFRIGERATED"},
    "REEFER": {"R", "REEFER", "REFRIGERATED"},
    "REFRIGERATED": {"R", "REEFER", "REFRIGERATED"},
    "F": {"F", "FLATBED", "FB"},
    "FB": {"F", "FLATBED", "FB"},
    "FLATBED": {"F", "FLATBED", "FB"},
    "SB": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B"},
    "BOX TRUCK": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B"},
    "STRAIGHT BOX": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B"},
    "HS": {"HS", "HOTSHOT", "HOT SHOT"},
    "HOTSHOT": {"HS", "HOTSHOT", "HOT SHOT"},
    "HOT SHOT": {"HS", "HOTSHOT", "HOT SHOT"},
    "PO": {"PO", "POWER ONLY"},
    "POWER ONLY": {"PO", "POWER ONLY"},
}


def _build_vehicle_distance_context(
    user: CurrentUser,
    tenant_cargo_dist: float | None,
    filters: LoadFilter,
) -> tuple[str, float, list, list]:
    vehicle_ids: list[int] = []
    if filters.vehicle_ids:
        vehicle_ids = [
            int(vid.strip())
            for vid in str(filters.vehicle_ids).split(",")
            if vid.strip().isdigit()
        ]

    has_vehicle_radius = False
    if filters.vehicle_radius is not None:
        try:
            has_vehicle_radius = float(filters.vehicle_radius) > 0 and bool(vehicle_ids)
        except (TypeError, ValueError):
            has_vehicle_radius = False

    has_radius = False
    if filters.radius is not None:
        try:
            has_radius = float(filters.radius) > 0
        except (TypeError, ValueError):
            has_radius = False

    has_matching = _is_truthy(filters.has_matching_vehicles)
    has_type_filter = bool(filters.vehicle_type)

    if has_vehicle_radius:
        mode = "vehicle"
        radius_miles = float(filters.vehicle_radius)  # type: ignore[arg-type]
    elif has_matching:
        mode = "matching"
        radius_miles = 300.0
        if tenant_cargo_dist is not None and tenant_cargo_dist != -1:
            radius_miles = float(tenant_cargo_dist)
        if has_radius:
            radius_miles = float(filters.radius)  # type: ignore[arg-type]
    elif has_radius:
        mode = "radius"
        radius_miles = float(filters.radius)  # type: ignore[arg-type]
    else:
        mode = "none"
        radius_miles = 0.0

    if mode == "none":
        dist_clauses = []
        if tenant_cargo_dist is not None and tenant_cargo_dist != -1:
            dist_clauses.append(Load.nearest_vehicles_count > 0)
        vehicle_cols = [
            Load.miles_out.label("miles_out"),
            Load.nearest_vehicles_count.label("nearest_vehicles_count"),
            literal(None, type_=Integer).label("miles_out_by_type"),
            literal(None, type_=Integer).label("nearest_vehicles_count_by_type"),
        ]
        return mode, radius_miles, vehicle_cols, dist_clauses

    radius_meters = radius_miles * 1609.344
    METERS_TO_MILES = 1.0 / 1609.344

    pickup_point = func.coalesce(
        Load.pick_up_location,
        func.cast(
            func.ST_SetSRID(
                func.ST_MakePoint(Load.pick_up_longitude, Load.pick_up_latitude),
                4326,
            ),
            Geography,
        ),
    )

    curr_valid = and_(
        Vehicle.latitude.is_not(None),
        Vehicle.longitude.is_not(None),
        Vehicle.location.is_not(None),
    )
    plan_valid = and_(
        Vehicle.planned_latitude.is_not(None),
        Vehicle.planned_longitude.is_not(None),
        Vehicle.planned_location.is_not(None),
        Vehicle.planned_address.is_not(None),
        Vehicle.planned_address != "",
    )

    curr_within = and_(curr_valid, func.ST_DWithin(Vehicle.location, pickup_point, radius_meters))
    plan_within = and_(plan_valid, func.ST_DWithin(Vehicle.planned_location, pickup_point, radius_meters))
    vehicle_in_radius = or_(curr_within, plan_within)

    curr_dist = func.ST_Distance(Vehicle.location, pickup_point) * METERS_TO_MILES
    plan_dist = func.ST_Distance(Vehicle.planned_location, pickup_point) * METERS_TO_MILES

    type_synonym_match = or_(
        func.upper(VehicleType.name) == func.upper(Load.vehicle_type),
        case(
            (func.upper(Load.vehicle_type) == "V", func.upper(VehicleType.name).in_(("V", "VAN", "DRY VAN"))),
            (func.upper(Load.vehicle_type) == "VAN", func.upper(VehicleType.name).in_(("V", "VAN", "DRY VAN"))),
            (func.upper(Load.vehicle_type) == "DRY VAN", func.upper(VehicleType.name).in_(("V", "VAN", "DRY VAN"))),
            (func.upper(Load.vehicle_type) == "R", func.upper(VehicleType.name).in_(("R", "REEFER", "REFRIGERATED"))),
            (func.upper(Load.vehicle_type) == "REEFER", func.upper(VehicleType.name).in_(("R", "REEFER", "REFRIGERATED"))),
            (func.upper(Load.vehicle_type) == "REFRIGERATED", func.upper(VehicleType.name).in_(("R", "REEFER", "REFRIGERATED"))),
            (func.upper(Load.vehicle_type) == "F", func.upper(VehicleType.name).in_(("F", "FLATBED", "FB"))),
            (func.upper(Load.vehicle_type) == "FB", func.upper(VehicleType.name).in_(("F", "FLATBED", "FB"))),
            (func.upper(Load.vehicle_type) == "FLATBED", func.upper(VehicleType.name).in_(("F", "FLATBED", "FB"))),
            (func.upper(Load.vehicle_type) == "SB", func.upper(VehicleType.name).in_(("SB", "STRAIGHT BOX", "BOX TRUCK", "B"))),
            (func.upper(Load.vehicle_type) == "BOX TRUCK", func.upper(VehicleType.name).in_(("SB", "STRAIGHT BOX", "BOX TRUCK", "B"))),
            (func.upper(Load.vehicle_type) == "STRAIGHT BOX", func.upper(VehicleType.name).in_(("SB", "STRAIGHT BOX", "BOX TRUCK", "B"))),
            (func.upper(Load.vehicle_type) == "HS", func.upper(VehicleType.name).in_(("HS", "HOTSHOT", "HOT SHOT"))),
            (func.upper(Load.vehicle_type) == "HOTSHOT", func.upper(VehicleType.name).in_(("HS", "HOTSHOT", "HOT SHOT"))),
            (func.upper(Load.vehicle_type) == "HOT SHOT", func.upper(VehicleType.name).in_(("HS", "HOTSHOT", "HOT SHOT"))),
            (func.upper(Load.vehicle_type) == "PO", func.upper(VehicleType.name).in_(("PO", "POWER ONLY"))),
            (func.upper(Load.vehicle_type) == "POWER ONLY", func.upper(VehicleType.name).in_(("PO", "POWER ONLY"))),
            else_=literal(False),
        ),
    )
    type_match = or_(
        Vehicle.type.has(type_synonym_match),
        Vehicle.types.any(type_synonym_match),
    )
    weight_match = or_(
        func.coalesce(Load.weight, 0) <= 0,
        and_(Vehicle.payload_lbs.is_not(None), Vehicle.payload_lbs >= Load.weight),
    )

    user_pool_cond = and_(
        Vehicle.status == 1,
        Vehicle.registration_status == 4,
        Vehicle.is_deleted.is_(False),
    )
    if user.team_ids:
        user_pool_cond = and_(
            user_pool_cond,
            or_(Vehicle.team_id.in_(user.team_ids), Vehicle.team_id.is_(None)),
        )

    if filters.vehicle_types:
        raw_vtypes = [t.strip() for t in filters.vehicle_types.split(",") if t.strip()]
        if raw_vtypes:
            matching_vtypes = set(t.upper() for t in raw_vtypes) | set(raw_vtypes)
            for vt in list(matching_vtypes):
                if vt in TYPE_SYNONYMS:
                    matching_vtypes.update(TYPE_SYNONYMS[vt])
            vtypes_list = list(matching_vtypes)
            pool_vtypes_cond = or_(
                Vehicle.type.has(func.upper(VehicleType.name).in_(vtypes_list)),
                Vehicle.types.any(func.upper(VehicleType.name).in_(vtypes_list)),
            )
            user_pool_cond = and_(user_pool_cond, pool_vtypes_cond)

    dist_clauses = []
    if mode == "vehicle":
        selected_pool_cond = and_(
            Vehicle.id.in_(vehicle_ids),
            Vehicle.is_deleted.is_(False),
        )
        show_only_selected = _is_truthy(filters.show_only_selected)
        if show_only_selected:
            stats_pool_cond = selected_pool_cond
        else:
            stats_pool_cond = or_(selected_pool_cond, user_pool_cond)

        if has_matching:
            dist_clauses.append(
                exists(
                    select(Vehicle.id).where(
                        selected_pool_cond,
                        vehicle_in_radius,
                        weight_match,
                        type_match,
                    )
                )
            )
        else:
            dist_clauses.append(exists(select(Vehicle.id).where(selected_pool_cond, vehicle_in_radius)))
            if has_type_filter:
                dist_clauses.append(
                    exists(
                        select(Vehicle.id).where(
                            selected_pool_cond,
                            vehicle_in_radius,
                            type_match,
                        )
                    )
                )
    elif mode == "matching":
        stats_pool_cond = user_pool_cond
        dist_clauses.append(
            exists(
                select(Vehicle.id).where(
                    stats_pool_cond,
                    vehicle_in_radius,
                    weight_match,
                    type_match,
                )
            )
        )
    elif mode == "radius":
        stats_pool_cond = user_pool_cond
        dist_clauses.append(exists(select(Vehicle.id).where(stats_pool_cond, vehicle_in_radius)))
        if has_type_filter:
            dist_clauses.append(
                exists(
                    select(Vehicle.id).where(
                        stats_pool_cond,
                        vehicle_in_radius,
                        type_match,
                    )
                )
            )

    if has_matching:
        stats_calc_cond = and_(stats_pool_cond, weight_match)
    else:
        stats_calc_cond = stats_pool_cond

    curr_nearest_sub = (
        select(func.round(curr_dist))
        .where(stats_calc_cond, curr_within)
        .order_by(curr_dist)
        .limit(1)
        .scalar_subquery()
    )
    plan_nearest_sub = (
        select(func.round(plan_dist))
        .where(stats_calc_cond, plan_within)
        .order_by(plan_dist)
        .limit(1)
        .scalar_subquery()
    )
    nearest_miles_expr = func.least(
        func.coalesce(curr_nearest_sub, 10_000_000),
        func.coalesce(plan_nearest_sub, 10_000_000),
    )
    miles_out_col = case(
        (nearest_miles_expr >= 10_000_000, 0),
        else_=cast(nearest_miles_expr, Integer),
    ).label("miles_out")

    curr_cnt_sub = (
        select(func.count(Vehicle.id))
        .where(stats_calc_cond, curr_within)
        .scalar_subquery()
    )
    plan_cnt_sub = (
        select(func.count(Vehicle.id))
        .where(stats_calc_cond, plan_within)
        .scalar_subquery()
    )
    nearest_vehicles_count_col = (
        func.coalesce(curr_cnt_sub, 0) + func.coalesce(plan_cnt_sub, 0)
    ).label("nearest_vehicles_count")

    include_by_type = has_matching or has_type_filter
    if include_by_type:
        stats_typed_cond = and_(stats_calc_cond, type_match)

        curr_typed_nearest_sub = (
            select(func.round(curr_dist))
            .where(stats_typed_cond, curr_within)
            .order_by(curr_dist)
            .limit(1)
            .scalar_subquery()
        )
        plan_typed_nearest_sub = (
            select(func.round(plan_dist))
            .where(stats_typed_cond, plan_within)
            .order_by(plan_dist)
            .limit(1)
            .scalar_subquery()
        )
        nearest_typed_miles_expr = func.least(
            func.coalesce(curr_typed_nearest_sub, 10_000_000),
            func.coalesce(plan_typed_nearest_sub, 10_000_000),
        )
        miles_out_by_type_col = case(
            (nearest_typed_miles_expr >= 10_000_000, 0),
            else_=cast(nearest_typed_miles_expr, Integer),
        ).label("miles_out_by_type")

        curr_typed_cnt_sub = (
            select(func.count(Vehicle.id))
            .where(stats_typed_cond, curr_within)
            .scalar_subquery()
        )
        plan_typed_cnt_sub = (
            select(func.count(Vehicle.id))
            .where(stats_typed_cond, plan_within)
            .scalar_subquery()
        )
        nearest_vehicles_count_by_type_col = (
            func.coalesce(curr_typed_cnt_sub, 0) + func.coalesce(plan_typed_cnt_sub, 0)
        ).label("nearest_vehicles_count_by_type")
    else:
        miles_out_by_type_col = literal(0, type_=Integer).label("miles_out_by_type")
        nearest_vehicles_count_by_type_col = literal(0, type_=Integer).label("nearest_vehicles_count_by_type")

    vehicle_cols = [
        miles_out_col,
        nearest_vehicles_count_col,
        miles_out_by_type_col,
        nearest_vehicles_count_by_type_col,
    ]
    return mode, radius_miles, vehicle_cols, dist_clauses


@dataclass
class LoadListParams:
    cargo_distance: float | None = None
    page: int = 1
    page_size: int = 20


class LoadListService:
    def __init__(
        self,
        session: AsyncSession,
        user: CurrentUser,
        tenant: TenantCompanyOut | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.tenant = tenant
        self.team_ids = user.team_ids

    async def list(
        self, params: LoadListParams, filters: LoadFilter
    ) -> tuple[int, list[LoadListSchema]]:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)

        tenant_cargo_dist = getattr(self.tenant, "cargo_distance", None) if self.tenant else None
        mode, radius_miles, vehicle_cols, dist_clauses = _build_vehicle_distance_context(
            user=self.user,
            tenant_cargo_dist=tenant_cargo_dist,
            filters=filters,
        )

        resolved_radius: float | None = None
        if mode != "none":
            resolved_radius = radius_miles
        elif params.cargo_distance is not None:
            try:
                resolved_radius = float(params.cargo_distance)
            except (ValueError, TypeError):
                resolved_radius = None
        elif tenant_cargo_dist is not None and tenant_cargo_dist != -1:
            resolved_radius = float(tenant_cargo_dist)

        clauses = [Load.is_active.is_(True), Load.is_deleted.is_(False)]
        clauses.extend(dist_clauses)

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

        vehicle_scope = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]
        if self.team_ids:
            vehicle_scope.append(
                or_(
                    Vehicle.team_id.in_(self.team_ids),
                    Vehicle.team_id.is_(None),
                )
            )

        is_bid_col = exists(
            select(Bid.id)
            .join(Vehicle, Vehicle.id == Bid.vehicle_id)
            .where(
                Bid.load_id == Load.id,
                Bid.is_deleted.is_(False),
                Bid.created_at >= cutoff,
                *vehicle_scope,
            )
        )
        is_driver_bid_col = exists(
            select(DriverBid.id)
            .join(Vehicle, Vehicle.id == DriverBid.vehicle_id)
            .where(
                DriverBid.load_id == Load.id,
                DriverBid.dispatch_bid_date.is_(None),
                DriverBid.is_deleted.is_(False),
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
            is_pinned_col = exists(
                select(load_pinned_users.c.id).where(
                    load_pinned_users.c.load_id == Load.id,
                    load_pinned_users.c.user_id == self.user.user_id,
                )
            )
        else:
            is_read_col = literal(False)
            is_pinned_col = literal(False)

        # django-filter clauses
        clauses.extend(filters.conditions())
        if filters.is_driver_bid and str(filters.is_driver_bid).lower() in ("true", "1", "t", "yes", "y"):
            clauses.append(is_driver_bid_col)

        where = and_(*clauses)

        count = await self.session.scalar(
            select(func.count()).select_from(Load).where(where)
        )
        if not count:
            return 0, []

        cols = [
            Load,
            is_bid_col.label("is_bid"),
            is_driver_bid_col.label("is_driver_bid"),
            is_read_col.label("is_read"),
            is_pinned_col.label("is_pinned"),
            *vehicle_cols,
        ]

        stmt = (
            select(*cols)
            .where(where)
            .order_by(
                is_pinned_col.desc(),
                Load.received_date.desc().nulls_last(),
                Load.id.desc(),
            )
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
            .options(
                joinedload(Load.broker_company),
                selectinload(Load.vehicle_teams),
            )
        )
        rows = (await self.session.execute(stmt)).unique().all()

        results: list[LoadListSchema] = [
            LoadListSchema.from_load(
                row[0],
                is_bid=bool(row.is_bid),
                is_driver_bid=bool(row.is_driver_bid),
                is_read=bool(row.is_read),
                is_pinned=bool(row.is_pinned),
                radius=resolved_radius,
                miles_out=row.miles_out,
                nearest_vehicles_count=row.nearest_vehicles_count,
                miles_out_by_type=row.miles_out_by_type,
                nearest_vehicles_count_by_type=row.nearest_vehicles_count_by_type,
            )
            for row in rows
        ]
        return int(count), results

class LoadDetailService:
    def __init__(self, session: AsyncSession, user: CurrentUser, tenant: TenantCompanyOut | None) -> None:
        self.session = session
        self.user = user
        self.tenant: TenantCompanyOut | None = tenant

    async def get(
            self, 
            load_id: int,
        ) -> LoadDetailSchema | None:
        load = await self.session.scalar(
            select(Load)
            .where(Load.id == load_id, Load.is_deleted.is_(False))
            .options(
                joinedload(Load.broker_company),
                selectinload(Load.points),
            )
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
                    Team.name.label("team_name"),
                    Vehicle.object_id,
                    Vehicle.driver_id,
                )
                .select_from(Bid)
                .join(Vehicle, Vehicle.id == Bid.vehicle_id, isouter=True)
                .join(Team, Team.id == Vehicle.team_id, isouter=True)
                .where(Bid.load_id == load.id)
                .order_by(Bid.id.desc())
            )
            if view_mode == "own":
                stmt = stmt.where(Bid.dispatcher_id == self.user.user_id)
            if self.user.team_ids:
                stmt = stmt.where(
                    or_(
                        Vehicle.team_id.is_(None),
                        Vehicle.team_id.in_(self.user.team_ids),
                    )
                )

            rows = (await self.session.execute(stmt)).mappings().all()

            dispatcher_ids = [row.get("dispatcher_id") for row in rows if row.get("dispatcher_id") is not None]
            driver_ids = [row.get("driver_id") for row in rows if row.get("driver_id") is not None]

            dispatcher_names: dict[int, str | None] = {}
            if dispatcher_ids:
                dispatcher_ids_unique = tuple(dict.fromkeys(dispatcher_ids))
                dispatcher_rows = await self.session.execute(
                    text(
                        """
                        SELECT id, first_name, last_name
                        FROM user_user
                        WHERE id = ANY(:dispatcher_ids)
                        """
                    ),
                    {"dispatcher_ids": list(dispatcher_ids_unique)},
                )
                for dispatcher_row in dispatcher_rows:
                    first_name = dispatcher_row.first_name or ""
                    last_name = dispatcher_row.last_name or ""
                    dispatcher_names[int(dispatcher_row.id)] = (
                        " ".join(part for part in [first_name, last_name] if part).strip() or None
                    )

            drivers_by_id: dict[int, Driver] = {}
            if driver_ids:
                driver_rows = await self.session.scalars(
                    select(Driver).where(Driver.id.in_(tuple(dict.fromkeys(driver_ids))))
                )
                for driver in driver_rows.all():
                    drivers_by_id[int(driver.id)] = driver

            result = []
            for row in rows:
                show_prices = (
                    view_mode == "with_prices"
                    or view_mode == "own"
                    or (view_mode == "without_prices" and row.get("dispatcher_id") == self.user.user_id)
                )

                dispatcher_name = None
                driver_name = None

                dispatcher_id = row.get("dispatcher_id")
                if dispatcher_id is not None:
                    dispatcher_name = dispatcher_names.get(int(dispatcher_id))

                driver_id = row.get("driver_id")
                if driver_id is not None:
                    driver = drivers_by_id.get(int(driver_id))
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
                        team=row.get("team_id"),
                        team_name=row.get("team_name"),
                    )
                )

            bid_info = result or None

        await self._mark_read(load_id)

        return LoadDetailSchema.from_load(
            load,
            bid_info=bid_info,
        )

    async def _mark_read(self, load_id: int) -> None:
        if self.user.user_id is None:
            return

        # Single atomic upsert — no SELECT round-trip and no duplicate race
        # (the M2M through-table carries UNIQUE(load_id, user_id)).
        result = await self.session.execute(
            pg_insert(load_is_read_users)
            .values(load_id=load_id, user_id=self.user.user_id)
            .on_conflict_do_nothing()
        )
        await self.session.commit()

        # Fire-and-forget so the websocket hop never delays the response.
        if result.rowcount and self.user.user_uuid is not None:
            task = asyncio.create_task(
                SenderToWebSocket().send_is_read_load(load_id, self.user.user_uuid)
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    async def get_info(
        self,
        load_id: int,
        filters: LoadFilter,
    ) -> LoadDetailInfoSchema | None:
        clauses = [
            Load.id == load_id,
            Load.is_active.is_(True),
            Load.is_deleted.is_(False),
        ]
        clauses.extend(filters.conditions())

        tenant_cargo_dist = getattr(self.tenant, "cargo_distance", None) if self.tenant else None
        mode, _, vehicle_cols, dist_clauses = _build_vehicle_distance_context(
            user=self.user,
            tenant_cargo_dist=tenant_cargo_dist,
            filters=filters,
        )
        clauses.extend(dist_clauses)

        cols = [
            Load.id,
            *vehicle_cols,
        ]
        stmt = select(*cols).where(and_(*clauses))
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None

        # Double check: if has_matching or mode != "none", verify counts
        has_matching = _is_truthy(filters.has_matching_vehicles)
        if has_matching and (not row.nearest_vehicles_count_by_type or row.nearest_vehicles_count_by_type == 0):
            return None

        has_vehicle_radius = (
            filters.vehicle_radius is not None
            and float(filters.vehicle_radius) > 0
            and bool(filters.vehicle_ids)
        )
        if has_vehicle_radius and (not row.nearest_vehicles_count or row.nearest_vehicles_count == 0):
            return None

        has_radius = filters.radius is not None and float(filters.radius) > 0
        if has_radius and (not row.nearest_vehicles_count or row.nearest_vehicles_count == 0):
            return None

        if filters.vehicle_type and (not row.nearest_vehicles_count_by_type or row.nearest_vehicles_count_by_type == 0):
            return None

        return LoadDetailInfoSchema(
            id=row.id,
            miles_out=row.miles_out or 0,
            nearest_vehicles_count=row.nearest_vehicles_count or 0,
            miles_out_by_type=row.miles_out_by_type or 0,
            nearest_vehicles_count_by_type=row.nearest_vehicles_count_by_type or 0,
        )



EARTH_RADIUS_MILES = 3958.756


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad_lat1 = math.radians(lat1)
    rad_lon1 = math.radians(lon1)
    rad_lat2 = math.radians(lat2)
    rad_lon2 = math.radians(lon2)

    dlat = rad_lat2 - rad_lat1
    dlon = rad_lon2 - rad_lon1

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(rad_lat1) * math.cos(rad_lat2) * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.asin(min(1.0, math.sqrt(max(0.0, a))))
    return EARTH_RADIUS_MILES * c



def _match_single_vehicle_type(load_type_raw: str, vt_single: str) -> bool:
    lt = load_type_raw.strip().upper()
    vt = vt_single.strip().upper()
    if lt == vt:
        return True

    load_types = {t.strip().upper() for t in re.split(r"[,/|]", load_type_raw) if t.strip()}
    v_synonyms = TYPE_SYNONYMS.get(vt, {vt})

    for t in load_types:
        if t == vt:
            return True
        t_synonyms = TYPE_SYNONYMS.get(t, {t})
        if not t_synonyms.isdisjoint(v_synonyms):
            return True
        if len(t) >= 4 and len(vt) >= 4 and (t in vt or vt in t):
            return True

    return False


def _match_vehicle_type(
    load_type_raw: str | None,
    vehicle_type_raw: str | list[str] | set[str] | tuple[str, ...] | None,
) -> bool:
    if not load_type_raw or not vehicle_type_raw:
        return False
    if isinstance(vehicle_type_raw, (list, set, tuple)):
        return any(_match_single_vehicle_type(load_type_raw, vt) for vt in vehicle_type_raw if vt)
    return _match_single_vehicle_type(load_type_raw, vehicle_type_raw)


def _match_weight(load_weight: int | float | None, vehicle_payload: float | None) -> bool:
    if load_weight is None or load_weight <= 0:
        return True
    if vehicle_payload is None:
        return False
    return vehicle_payload >= load_weight


