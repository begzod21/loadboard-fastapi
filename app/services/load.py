from __future__ import annotations
import asyncio
import datetime
import logging
import math
from dataclasses import dataclass

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload, selectinload
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
from ..models.vehicle import Driver, Vehicle, VehicleType
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
            .options(
                joinedload(Load.broker_company),
                selectinload(Load.vehicle_teams),
            )
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
        load = await self.session.scalar(
            select(Load).where(Load.id == load_id, Load.is_deleted.is_(False))
        )
        if load is None:
            return None

        # Determine distance mode
        vehicle_ids: list[int] = []
        if filters.vehicle_ids:
            vehicle_ids = [
                int(vid.strip())
                for vid in str(filters.vehicle_ids).split(",")
                if vid.strip().isdigit()
            ]

        has_vehicle_radius = (
            filters.vehicle_radius is not None
            and filters.vehicle_radius > 0
            and bool(vehicle_ids)
        )
        has_radius = filters.radius is not None and filters.radius > 0
        has_matching = _is_truthy(filters.has_matching_vehicles)

        if has_vehicle_radius:
            mode = "vehicle"
            radius_miles = float(filters.vehicle_radius)  # type: ignore[arg-type]
        elif has_radius:
            mode = "radius"
            radius_miles = float(filters.radius)  # type: ignore[arg-type]
        elif has_matching:
            mode = "matching"
            radius_miles = 300.0
            if (
                self.tenant is not None
                and self.tenant.cargo_distance is not None
                and self.tenant.cargo_distance != -1
            ):
                radius_miles = float(self.tenant.cargo_distance)
            if filters.radius is not None and filters.radius > 0:
                radius_miles = float(filters.radius)
        elif (
            self.tenant is not None
            and self.tenant.cargo_distance is not None
            and self.tenant.cargo_distance != -1
        ):
            mode = "radius"
            radius_miles = float(self.tenant.cargo_distance)
        else:
            mode = "none"
            radius_miles = 0.0

        if mode == "none":
            return LoadDetailInfoSchema(
                id=load.id,
                miles_out=load.miles_out or 0,
                nearest_vehicles_count=load.nearest_vehicles_count or 0,
                miles_out_by_type=0,
                nearest_vehicles_count_by_type=0,
            )

        if load.pick_up_latitude is None or load.pick_up_longitude is None:
            return LoadDetailInfoSchema(
                id=load.id,
                miles_out=0,
                nearest_vehicles_count=0,
                miles_out_by_type=0,
                nearest_vehicles_count_by_type=0,
            )

        load_lat = float(load.pick_up_latitude)
        load_lon = float(load.pick_up_longitude)

        v_query = (
            select(
                Vehicle.id,
                Vehicle.latitude,
                Vehicle.longitude,
                Vehicle.planned_latitude,
                Vehicle.planned_longitude,
                Vehicle.planned_address,
                Vehicle.payload_lbs,
                VehicleType.name.label("type_name"),
            )
            .join(VehicleType, VehicleType.id == Vehicle.type_id, isouter=True)
            .where(
                Vehicle.status == 1,
                Vehicle.registration_status.in_([1, 4]),
                Vehicle.is_deleted.is_(False),
            )
        )

        show_only_selected = _is_truthy(filters.show_only_selected)
        user_team_ids = [t for t in self.user.team_ids if t is not None]

        if mode == "vehicle":
            if show_only_selected:
                v_query = v_query.where(Vehicle.id.in_(vehicle_ids))
            else:
                if user_team_ids and not self.user.is_superuser:
                    v_query = v_query.where(
                        or_(
                            Vehicle.id.in_(vehicle_ids),
                            Vehicle.team_id.in_(user_team_ids),
                            Vehicle.team_id.is_(None),
                        )
                    )
        else:
            if user_team_ids and not self.user.is_superuser:
                v_query = v_query.where(
                    or_(
                        Vehicle.team_id.in_(user_team_ids),
                        Vehicle.team_id.is_(None),
                    )
                )
            if filters.vehicle_types:
                type_names = [
                    t.strip() for t in filters.vehicle_types.split(",") if t.strip()
                ]
                if type_names:
                    v_query = v_query.where(
                        or_(
                            VehicleType.name.in_(type_names),
                            func.upper(VehicleType.name).in_(
                                [n.upper() for n in type_names]
                            ),
                        )
                    )

        vehicle_rows = (await self.session.execute(v_query)).all()

        load_type_raw = filters.vehicle_type or load.vehicle_type
        load_weight = load.weight or 0

        stats_distances: list[int] = []
        current_in_radius_count = 0
        planned_in_radius_count = 0

        stats_typed_distances: list[int] = []
        current_typed_in_radius_count = 0
        planned_typed_in_radius_count = 0

        for row in vehicle_rows:
            v_lat = float(row.latitude) if row.latitude is not None else None
            v_lon = float(row.longitude) if row.longitude is not None else None
            v_plat = (
                float(row.planned_latitude)
                if row.planned_latitude is not None
                else None
            )
            v_plon = (
                float(row.planned_longitude)
                if row.planned_longitude is not None
                else None
            )
            v_payload = float(row.payload_lbs) if row.payload_lbs is not None else None
            v_type_name = row.type_name

            curr_valid = v_lat is not None and v_lon is not None
            plan_valid = v_plat is not None and v_plon is not None

            d_curr = (
                haversine_distance(v_lat, v_lon, load_lat, load_lon)
                if curr_valid
                else None
            )
            d_plan = (
                haversine_distance(v_plat, v_plon, load_lat, load_lon)
                if plan_valid
                else None
            )

            curr_in_radius = d_curr is not None and d_curr <= radius_miles
            plan_in_radius = d_plan is not None and d_plan <= radius_miles

            if not curr_in_radius and not plan_in_radius:
                continue

            type_matches = _match_vehicle_type(load_type_raw, v_type_name)
            weight_matches = _match_weight(load_weight, v_payload)

            # In has_matching mode, only consider vehicles that can carry the load's weight
            if has_matching and not weight_matches:
                continue

            if curr_in_radius:
                stats_distances.append(round(d_curr))  # type: ignore[arg-type]
                current_in_radius_count += 1
                if type_matches:
                    stats_typed_distances.append(round(d_curr))  # type: ignore[arg-type]
                    current_typed_in_radius_count += 1

            if plan_in_radius:
                stats_distances.append(round(d_plan))  # type: ignore[arg-type]
                planned_in_radius_count += 1
                if type_matches:
                    stats_typed_distances.append(round(d_plan))  # type: ignore[arg-type]
                    planned_typed_in_radius_count += 1

        nearest_miles = min(stats_distances) if stats_distances else 0
        nearest_vehicles_count = current_in_radius_count + planned_in_radius_count

        miles_out_by_type = (
            min(stats_typed_distances) if stats_typed_distances else 0
        )
        nearest_vehicles_count_by_type = (
            current_typed_in_radius_count + planned_typed_in_radius_count
        )

        return LoadDetailInfoSchema(
            id=load.id,
            miles_out=nearest_miles,
            nearest_vehicles_count=nearest_vehicles_count,
            miles_out_by_type=miles_out_by_type,
            nearest_vehicles_count_by_type=nearest_vehicles_count_by_type,
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


def _match_vehicle_type(load_type_raw: str | None, vehicle_type_raw: str | None) -> bool:
    if not load_type_raw or not vehicle_type_raw:
        return False

    lt = load_type_raw.strip().upper()
    vt = vehicle_type_raw.strip().upper()
    if lt == vt:
        return True

    load_types = {t.strip().upper() for t in load_type_raw.split(",") if t.strip()}
    v_synonyms = TYPE_SYNONYMS.get(vt, {vt})

    for t in load_types:
        if t == vt:
            return True
        t_synonyms = TYPE_SYNONYMS.get(t, {t})
        if not t_synonyms.isdisjoint(v_synonyms):
            return True
        if t in vt or vt in t:
            return True

    return False


def _match_weight(load_weight: int | float | None, vehicle_payload: float | None) -> bool:
    if load_weight is None or load_weight <= 0:
        return True
    if vehicle_payload is None:
        return False
    return vehicle_payload >= load_weight


