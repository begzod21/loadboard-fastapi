from __future__ import annotations

import asyncio
import datetime
import decimal
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
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

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
    "V": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "VAN": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "DRY VAN": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "CARGO VAN": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "SPRINTER": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "SPRINTER VAN": {"V", "VAN", "DRY VAN", "CARGO VAN", "SPRINTER"},
    "R": {"R", "REEFER", "REFRIGERATED"},
    "REEFER": {"R", "REEFER", "REFRIGERATED"},
    "REFRIGERATED": {"R", "REEFER", "REFRIGERATED"},
    "F": {"F", "FLATBED", "FB"},
    "FB": {"F", "FLATBED", "FB"},
    "FLATBED": {"F", "FLATBED", "FB"},
    "SB": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B", "STRAIGHT TRUCK"},
    "BOX TRUCK": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B", "STRAIGHT TRUCK"},
    "STRAIGHT BOX": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B", "STRAIGHT TRUCK"},
    "STRAIGHT TRUCK": {"SB", "STRAIGHT BOX", "BOX TRUCK", "B", "STRAIGHT TRUCK"},
    "HS": {"HS", "HOTSHOT", "HOT SHOT"},
    "HOTSHOT": {"HS", "HOTSHOT", "HOT SHOT"},
    "HOT SHOT": {"HS", "HOTSHOT", "HOT SHOT"},
    "PO": {"PO", "POWER ONLY"},
    "POWER ONLY": {"PO", "POWER ONLY"},
}


def _get_type_family(type_str: str) -> set[str]:
    s = type_str.strip().upper()
    family = set(TYPE_SYNONYMS.get(s, {s}))
    words = re.findall(r"\b[A-Z0-9]+\b", s)
    for w in words:
        if w in TYPE_SYNONYMS:
            family.update(TYPE_SYNONYMS[w])
    return family


def _match_single_vehicle_type(load_type_raw: str, vt_single: str) -> bool:
    lt = load_type_raw.strip().upper()
    vt = vt_single.strip().upper()
    if lt == vt:
        return True

    load_subtypes = [t.strip().upper() for t in re.split(r"[,/|]", lt) if t.strip()]
    v_family = _get_type_family(vt)

    for st in load_subtypes:
        if st == vt:
            return True
        st_family = _get_type_family(st)
        if not st_family.isdisjoint(v_family):
            return True
        if len(st) >= 4 and len(vt) >= 4 and (st in vt or vt in st):
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


def _compute_bboxes(points: list[tuple[float, float]], radius_miles: float) -> list[tuple[float, float, float, float]]:
    if not points:
        return []
    dlat = radius_miles / 68.0
    raw_boxes: list[tuple[float, float, float, float]] = []
    for lat, lon in points:
        cos_lat = max(math.cos(math.radians(abs(lat))), 0.2)
        dlon = radius_miles / (68.0 * cos_lat)
        raw_boxes.append((lat - dlat, lat + dlat, lon - dlon, lon + dlon))

    merged: list[tuple[float, float, float, float]] = []
    for b in raw_boxes:
        min_lat, max_lat, min_lon, max_lon = b
        combined = False
        for i, (m_lat1, m_lat2, m_lon1, m_lon2) in enumerate(merged):
            if not (max_lat < m_lat1 or min_lat > m_lat2 or max_lon < m_lon1 or min_lon > m_lon2):
                merged[i] = (
                    min(min_lat, m_lat1),
                    max(max_lat, m_lat2),
                    min(min_lon, m_lon1),
                    max(max_lon, m_lon2),
                )
                combined = True
                break
        if not combined:
            merged.append(b)
    return merged


@dataclass
class LoadDistanceStats:
    miles_out: int
    nearest_vehicles_count: int
    miles_out_by_type: int
    nearest_vehicles_count_by_type: int
    received_date: datetime.datetime | None = None


@dataclass
class VehiclePoolItem:
    id: int
    curr_pos: tuple[float, float] | None
    plan_pos: tuple[float, float] | None
    payload: float | None
    types: set[str]
    is_selected: bool


@dataclass
class LoadListParams:
    cargo_distance: float | None = None
    page: int = 1
    page_size: int = 20


def _parse_distance_params(
    tenant_cargo_dist: float | None,
    filters: LoadFilter,
) -> tuple[str, float, bool, bool, list[int]]:
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

    return mode, radius_miles, has_matching, has_type_filter, vehicle_ids


async def _resolve_vehicle_pool(
    session: AsyncSession,
    user: CurrentUser,
    filters: LoadFilter,
    mode: str,
    vehicle_ids: list[int],
) -> list[VehiclePoolItem]:
    v_scope = [
        Vehicle.status == 1,
        Vehicle.registration_status == 4,
        Vehicle.is_deleted.is_(False),
    ]
    if user.team_ids:
        v_scope.append(
            or_(Vehicle.team_id.in_(user.team_ids), Vehicle.team_id.is_(None))
        )
    user_v_cond = and_(*v_scope)

    if mode == "vehicle":
        selected_v_cond = and_(Vehicle.id.in_(vehicle_ids), Vehicle.is_deleted.is_(False))
        show_only_selected = _is_truthy(filters.show_only_selected)
        if show_only_selected:
            v_query_cond = selected_v_cond
        else:
            v_query_cond = or_(selected_v_cond, user_v_cond)
    else:
        v_query_cond = user_v_cond

    v_stmt = (
        select(Vehicle)
        .options(
            joinedload(Vehicle.type),
            selectinload(Vehicle.types),
        )
        .where(v_query_cond)
    )
    db_vehicles = (await session.execute(v_stmt)).unique().scalars().all()

    req_family: set[str] = set()
    if filters.vehicle_types:
        req_types = [t.strip() for t in filters.vehicle_types.split(",") if t.strip()]
        for rt in req_types:
            req_family.update(_get_type_family(rt))

    pool: list[VehiclePoolItem] = []
    selected_set = set(vehicle_ids) if mode == "vehicle" else set()

    for v in db_vehicles:
        v_types: set[str] = set()
        if v.type and v.type.name:
            v_types.add(v.type.name.strip().upper())
        for vt in (v.types or []):
            if vt and vt.name:
                v_types.add(vt.name.strip().upper())

        if req_family:
            v_fam: set[str] = set()
            for t in v_types:
                v_fam.update(_get_type_family(t))
            if v_fam.isdisjoint(req_family):
                continue

        curr_pos = None
        if v.latitude is not None and v.longitude is not None:
            try:
                curr_pos = (float(v.latitude), float(v.longitude))
            except (TypeError, ValueError):
                curr_pos = None

        plan_pos = None
        if (
            v.planned_latitude is not None
            and v.planned_longitude is not None
            and v.planned_address
            and str(v.planned_address).strip() != ""
        ):
            try:
                plan_pos = (float(v.planned_latitude), float(v.planned_longitude))
            except (TypeError, ValueError):
                plan_pos = None

        is_selected = (v.id in selected_set) if mode == "vehicle" else True

        pool.append(
            VehiclePoolItem(
                id=v.id,
                curr_pos=curr_pos,
                plan_pos=plan_pos,
                payload=v.payload_lbs,
                types=v_types,
                is_selected=is_selected,
            )
        )
    return pool


def _evaluate_load_distances(
    load_lat: float,
    load_lon: float,
    load_vtype: str | None,
    load_weight: int | float | None,
    vehicle_pool: list[VehiclePoolItem],
    radius_miles: float,
    mode: str,
    has_matching: bool,
    has_type_filter: bool,
) -> tuple[bool, LoadDistanceStats | None]:
    nearest_miles = float("inf")
    vehicles_count = 0
    nearest_typed_miles = float("inf")
    vehicles_count_by_type = 0

    selected_in_radius = False
    selected_typed_in_radius = False

    for v in vehicle_pool:
        weight_ok = _match_weight(load_weight, v.payload)
        type_ok = _match_vehicle_type(load_vtype, v.types)
        counts_for_stats = (not has_matching) or weight_ok

        v_in_radius = False
        if v.curr_pos:
            d_curr = haversine_distance(load_lat, load_lon, v.curr_pos[0], v.curr_pos[1])
            if d_curr <= radius_miles:
                v_in_radius = True
                if counts_for_stats:
                    vehicles_count += 1
                    if d_curr < nearest_miles:
                        nearest_miles = d_curr
                    if type_ok and weight_ok:
                        vehicles_count_by_type += 1
                        if d_curr < nearest_typed_miles:
                            nearest_typed_miles = d_curr

        if v.plan_pos:
            d_plan = haversine_distance(load_lat, load_lon, v.plan_pos[0], v.plan_pos[1])
            if d_plan <= radius_miles:
                v_in_radius = True
                if counts_for_stats:
                    vehicles_count += 1
                    if d_plan < nearest_miles:
                        nearest_miles = d_plan
                    if type_ok and weight_ok:
                        vehicles_count_by_type += 1
                        if d_plan < nearest_typed_miles:
                            nearest_typed_miles = d_plan

        if v_in_radius and v.is_selected:
            selected_in_radius = True
            if type_ok and weight_ok:
                selected_typed_in_radius = True
            elif type_ok and not has_matching:
                selected_typed_in_radius = True

    if mode == "vehicle":
        if has_matching:
            qualifies = selected_typed_in_radius
        elif has_type_filter:
            qualifies = selected_typed_in_radius
        else:
            qualifies = selected_in_radius
    elif mode == "matching":
        qualifies = (vehicles_count_by_type > 0)
    elif mode == "radius":
        if has_type_filter:
            qualifies = (vehicles_count_by_type > 0)
        else:
            qualifies = (vehicles_count > 0)
    else:
        qualifies = True

    if not qualifies:
        return False, None

    stats = LoadDistanceStats(
        miles_out=round(nearest_miles) if nearest_miles < float("inf") else 0,
        nearest_vehicles_count=vehicles_count,
        miles_out_by_type=round(nearest_typed_miles) if nearest_typed_miles < float("inf") else 0,
        nearest_vehicles_count_by_type=vehicles_count_by_type,
    )
    return True, stats


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

        mode, radius_miles, has_matching, has_type_filter, vehicle_ids = _parse_distance_params(
            tenant_cargo_dist, filters
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

        # -------------------------------------------------------------
        # Case A: mode == "none" (no vehicle distance filtering)
        # -------------------------------------------------------------
        if mode == "none":
            clauses = [Load.is_active.is_(True), Load.is_deleted.is_(False)]
            if tenant_cargo_dist is not None and tenant_cargo_dist != -1:
                clauses.append(Load.nearest_vehicles_count > 0)

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
                Load.miles_out.label("miles_out"),
                Load.nearest_vehicles_count.label("nearest_vehicles_count"),
                literal(None, type_=Integer).label("miles_out_by_type"),
                literal(None, type_=Integer).label("nearest_vehicles_count_by_type"),
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
            results = [
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

        # -------------------------------------------------------------
        # Case B: mode != "none" (matching, radius, vehicle)
        # -------------------------------------------------------------
        vehicle_pool = await _resolve_vehicle_pool(
            self.session, self.user, filters, mode, vehicle_ids
        )
        if not vehicle_pool:
            return 0, []

        points: list[tuple[float, float]] = []
        for v in vehicle_pool:
            if v.curr_pos:
                points.append(v.curr_pos)
            if v.plan_pos:
                points.append(v.plan_pos)

        if not points:
            return 0, []

        bboxes = _compute_bboxes(points, radius_miles)
        if not bboxes:
            return 0, []

        bbox_clauses = [
            and_(
                Load.pick_up_latitude.between(b[0], b[1]),
                Load.pick_up_longitude.between(b[2], b[3]),
            )
            for b in bboxes
        ]

        base_clauses = [
            Load.is_active.is_(True),
            Load.is_deleted.is_(False),
            or_(*bbox_clauses),
        ]
        if self.team_ids:
            has_team = exists(
                select(load_vehicle_teams.c.id).where(
                    load_vehicle_teams.c.load_id == Load.id,
                    load_vehicle_teams.c.team_id.in_(self.team_ids),
                )
            )
            base_clauses.append(
                or_(
                    Load.has_driver_in_all_teams.is_(True),
                    and_(Load.has_driver_in_all_teams.is_(False), has_team),
                )
            )

        base_clauses.extend(filters.conditions())
        if filters.is_driver_bid and str(filters.is_driver_bid).lower() in ("true", "1", "t", "yes", "y"):
            base_clauses.append(is_driver_bid_col)

        if has_matching:
            payloads = [v.payload for v in vehicle_pool if v.payload is not None]
            if payloads:
                max_payload = max(payloads)
                base_clauses.append(or_(Load.weight.is_(None), Load.weight <= max_payload))

        candidate_stmt = (
            select(
                Load.id,
                Load.pick_up_latitude,
                Load.pick_up_longitude,
                Load.vehicle_type,
                Load.weight,
                Load.received_date,
            )
            .where(and_(*base_clauses))
        )
        candidates = (await self.session.execute(candidate_stmt)).all()
        if not candidates:
            return 0, []

        matched_stats: dict[int, LoadDistanceStats] = {}
        for row in candidates:
            cid = row[0]
            clat = row[1]
            clon = row[2]
            cvtype = row[3]
            cweight = row[4]
            creceived = row[5]

            if clat is None or clon is None:
                continue

            try:
                flat_lat = float(clat)
                flat_lon = float(clon)
            except (TypeError, ValueError):
                continue

            qualifies, stats = _evaluate_load_distances(
                load_lat=flat_lat,
                load_lon=flat_lon,
                load_vtype=cvtype,
                load_weight=cweight,
                vehicle_pool=vehicle_pool,
                radius_miles=radius_miles,
                mode=mode,
                has_matching=has_matching,
                has_type_filter=has_type_filter,
            )
            if qualifies and stats is not None:
                stats.received_date = creceived
                matched_stats[cid] = stats

        if not matched_stats:
            return 0, []

        pinned_ids: set[int] = set()
        if self.user.user_id is not None:
            p_stmt = select(load_pinned_users.c.load_id).where(
                load_pinned_users.c.user_id == self.user.user_id,
                load_pinned_users.c.load_id.in_(list(matched_stats.keys())),
            )
            pinned_ids = set((await self.session.execute(p_stmt)).scalars().all())

        def sort_key(item: tuple[int, LoadDistanceStats]):
            lid, s = item
            is_pin = lid in pinned_ids
            dt = s.received_date
            return (
                0 if is_pin else 1,
                1 if dt is None else 0,
                -(dt.timestamp()) if dt else 0,
                -lid,
            )

        sorted_items = sorted(matched_stats.items(), key=sort_key)
        total_count = len(sorted_items)

        start = (params.page - 1) * params.page_size
        end = start + params.page_size
        page_items = sorted_items[start:end]
        page_ids = [item[0] for item in page_items]

        if not page_ids:
            return total_count, []

        page_stmt = (
            select(
                Load,
                is_bid_col.label("is_bid"),
                is_driver_bid_col.label("is_driver_bid"),
                is_read_col.label("is_read"),
            )
            .where(Load.id.in_(page_ids))
            .options(
                joinedload(Load.broker_company),
                selectinload(Load.vehicle_teams),
            )
        )
        rows = (await self.session.execute(page_stmt)).unique().all()
        row_map = {row[0].id: row for row in rows}

        results: list[LoadListSchema] = []
        for lid in page_ids:
            row = row_map.get(lid)
            if not row:
                continue
            stats = matched_stats[lid]
            results.append(
                LoadListSchema.from_load(
                    row[0],
                    is_bid=bool(row.is_bid),
                    is_driver_bid=bool(row.is_driver_bid),
                    is_read=bool(row.is_read),
                    is_pinned=(lid in pinned_ids),
                    radius=resolved_radius,
                    miles_out=stats.miles_out,
                    nearest_vehicles_count=stats.nearest_vehicles_count,
                    miles_out_by_type=stats.miles_out_by_type,
                    nearest_vehicles_count_by_type=stats.nearest_vehicles_count_by_type,
                )
            )

        return total_count, results


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

        load = await self.session.scalar(
            select(Load).where(and_(*clauses))
        )
        if load is None:
            return None

        tenant_cargo_dist = getattr(self.tenant, "cargo_distance", None) if self.tenant else None
        mode, radius_miles, has_matching, has_type_filter, vehicle_ids = _parse_distance_params(
            tenant_cargo_dist, filters
        )

        if mode == "none":
            return LoadDetailInfoSchema(
                id=load.id,
                miles_out=load.miles_out or 0,
                nearest_vehicles_count=load.nearest_vehicles_count or 0,
                miles_out_by_type=0,
                nearest_vehicles_count_by_type=0,
            )

        if load.pick_up_latitude is None or load.pick_up_longitude is None:
            return None

        try:
            flat_lat = float(load.pick_up_latitude)
            flat_lon = float(load.pick_up_longitude)
        except (TypeError, ValueError):
            return None

        vehicle_pool = await _resolve_vehicle_pool(
            self.session, self.user, filters, mode, vehicle_ids
        )
        if not vehicle_pool:
            return None

        qualifies, stats = _evaluate_load_distances(
            load_lat=flat_lat,
            load_lon=flat_lon,
            load_vtype=load.vehicle_type,
            load_weight=load.weight,
            vehicle_pool=vehicle_pool,
            radius_miles=radius_miles,
            mode=mode,
            has_matching=has_matching,
            has_type_filter=has_type_filter,
        )
        if not qualifies or stats is None:
            return None

        if has_matching and (not stats.nearest_vehicles_count_by_type or stats.nearest_vehicles_count_by_type == 0):
            return None

        return LoadDetailInfoSchema(
            id=load.id,
            miles_out=stats.miles_out,
            nearest_vehicles_count=stats.nearest_vehicles_count,
            miles_out_by_type=stats.miles_out_by_type,
            nearest_vehicles_count_by_type=stats.nearest_vehicles_count_by_type,
        )

