from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, radians

from sqlalchemy import (
    Float,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.security import CurrentUser
from ..filters.vehicle import VehicleFilter
from ..models.load import Bid, ConfirmedLoad, DriverBid, Load
from ..models.vehicle import Vehicle, VehicleType
from ..schemas.vehicle import VehicleSchema
from .mapbox import MapService


EARTH_RADIUS_MILES = 3958.756
MILES_PER_DEGREE_LATITUDE = 69.0

DEFAULT_RADIUS_MILES = 300.0
BBOX_MARGIN_DEGREES = 0.05


# ============================================================
# GEO
# ============================================================


def _bbox(
    lat_col,
    lon_col,
    lat: float,
    lon: float,
    radius_miles: float,
):
    """
    Cheap rectangular pre-filter.

    IMPORTANT:
    This is NOT the final distance check.
    It only reduces the number of rows for which Haversine
    has to be calculated.
    """
    lat_delta = radius_miles / MILES_PER_DEGREE_LATITUDE

    cos_lat = max(abs(cos(radians(lat))), 0.01)
    lon_delta = radius_miles / (MILES_PER_DEGREE_LATITUDE * cos_lat)

    lat_delta += BBOX_MARGIN_DEGREES
    lon_delta += BBOX_MARGIN_DEGREES

    return and_(
        lat_col >= lat - lat_delta,
        lat_col <= lat + lat_delta,
        lon_col >= lon - lon_delta,
        lon_col <= lon + lon_delta,
    )


def _haversine(
    lat_col,
    lon_col,
    lat: float,
    lon: float,
):
    """
    Haversine distance in miles.

    BBox MUST be applied before this expression.
    """
    lat_r = radians(lat)
    lon_r = radians(lon)

    lat_col_r = func.radians(cast(lat_col, Float))
    lon_col_r = func.radians(cast(lon_col, Float))

    inner = (
        func.cos(lat_r)
        * func.cos(lat_col_r)
        * func.cos(lon_col_r - lon_r)
        + func.sin(lat_r)
        * func.sin(lat_col_r)
    )

    return EARTH_RADIUS_MILES * func.acos(
        func.least(
            1.0,
            func.greatest(-1.0, inner),
        )
    )


# ============================================================
# PARAMS
# ============================================================


@dataclass(slots=True)
class VehicleListParams:
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    radius: float | None = None
    load_id: int | None = None
    bid_id: int | None = None

    vehicle_ids: list[int] = field(default_factory=list)

    show_only_selected: bool = False
    has_matching_vehicles: bool = False

    page: int = 1
    page_size: int = 20


# ============================================================
# SERVICE
# ============================================================


class VehicleListService:
    def __init__(
        self,
        session: AsyncSession,
        user: CurrentUser,
        mapbox_token: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
        self.team_ids = user.team_ids or []

        self.map_service = MapService(mapbox_token)

    # ========================================================
    # PUBLIC
    # ========================================================

    async def list(
        self,
        params: VehicleListParams,
        filters: VehicleFilter,
    ) -> tuple[int, list[VehicleSchema]]:
        latitude = params.latitude
        longitude = params.longitude

        # ----------------------------------------------------
        # Address
        # ----------------------------------------------------

        if params.address:
            longitude, latitude = await self.map_service.get_coordinates(
                params.address
            )
            if longitude is None or latitude is None:
                return 0, []

        # ----------------------------------------------------
        # Load / Bid
        # ----------------------------------------------------

        load: Load | None = None
        vehicle_id: int | None = None

        if params.load_id:
            load = await self.session.get(Load, params.load_id)
            if load is None:
                raise LookupError(f"Load not found! ID: {params.load_id}")

            if load.pick_up_latitude is not None and load.pick_up_longitude is not None:
                latitude = float(load.pick_up_latitude)
                longitude = float(load.pick_up_longitude)

        if params.bid_id:
            bid = await self.session.get(Bid, params.bid_id)
            if bid is None:
                raise LookupError(f"Bid not found! ID: {params.bid_id}")

            if not bid.load_id:
                raise LookupError("This bid has no Load!")

            load = await self.session.get(Load, bid.load_id)
            vehicle_id = bid.vehicle_id

            if load is not None:
                if load.pick_up_latitude is not None and load.pick_up_longitude is not None:
                    latitude = float(load.pick_up_latitude)
                    longitude = float(load.pick_up_longitude)

        # ----------------------------------------------------
        # Matching
        # ----------------------------------------------------

        matching_vehicle_type = None
        matching_weight = None

        if params.has_matching_vehicles and load is not None:
            matching_vehicle_type = load.vehicle_type if load.vehicle_type else None
            matching_weight = load.weight if load.weight is not None else None

        # ----------------------------------------------------
        # No coordinates
        # ----------------------------------------------------

        if latitude is None or longitude is None:
            return await self._plain_list(
                filters=filters,
                params=params,
                matching_vehicle_type=matching_vehicle_type,
                matching_weight=matching_weight,
            )

        # ----------------------------------------------------
        # Radius
        # ----------------------------------------------------

        radius = (
            DEFAULT_RADIUS_MILES
            if params.radius is None or params.radius < 0
            else params.radius
        )
        if radius <= 0:
            radius = DEFAULT_RADIUS_MILES

        load_id = (
            params.load_id
            if params.load_id
            else (load.id if params.bid_id and load is not None else None)
        )

        return await self._distance_list(
            lat=float(latitude),
            lon=float(longitude),
            radius=radius,
            vehicle_id=vehicle_id,
            is_bid=params.bid_id is not None,
            load_id=load_id,
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

    # ========================================================
    # BASE FILTER
    # ========================================================

    def _base_filter(
        self,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ):
        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        show_only_selected = params.show_only_selected and bool(params.vehicle_ids)

        if show_only_selected:
            conditions.append(Vehicle.id.in_(params.vehicle_ids))

        if matching_vehicle_type:
            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name) == matching_vehicle_type.upper()
                )
            )

        if matching_weight is not None:
            conditions.append(Vehicle.payload_lbs >= matching_weight)

        return and_(*conditions)

    # ========================================================
    # TEAM
    # ========================================================

    def _team_filter(
        self,
        *,
        is_bid: bool,
        vehicle_id: int | None,
    ):
        if not self.team_ids:
            return None

        condition = or_(
            Vehicle.team_id.in_(self.team_ids),
            Vehicle.team_id.is_(None),
        )

        if is_bid and vehicle_id:
            condition = or_(
                condition,
                Vehicle.id == vehicle_id,
            )

        return condition

    # ========================================================
    # PLAIN
    # ========================================================

    async def _plain_list(
        self,
        filters: VehicleFilter,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ):
        base = self._base_filter(params, matching_vehicle_type, matching_weight)
        combined = filters.combined()
        where = and_(base, combined) if combined is not None else base

        count = await self.session.scalar(
            select(func.count(Vehicle.id)).select_from(Vehicle).where(where)
        )

        show_only_selected = params.show_only_selected and bool(params.vehicle_ids)

        order_by = []

        if params.vehicle_ids and not show_only_selected:
            selected = case(
                (Vehicle.id.in_(params.vehicle_ids), 0),
                else_=1,
            )
            order_by.append(selected.asc())

        order_by.append(Vehicle.id.desc())

        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(*order_by)
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
            .options(selectinload(Vehicle.equipment))
        )

        vehicles = (await self.session.scalars(stmt)).unique().all()

        return (
            int(count or 0),
            [VehicleSchema.from_vehicle(vehicle) for vehicle in vehicles],
        )

    # ========================================================
    # DISTANCE
    # ========================================================

    async def _distance_list(
        self,
        *,
        lat: float,
        lon: float,
        radius: float,
        vehicle_id: int | None,
        is_bid: bool,
        load_id: int | None,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ):
        base = self._base_filter(params, matching_vehicle_type, matching_weight)
        team = self._team_filter(is_bid=is_bid, vehicle_id=vehicle_id)

        # ----------------------------------------------------
        # Driver bid EXISTS
        # ----------------------------------------------------

        if load_id:
            is_driver_bid_vehicle = exists(
                select(1)
                .select_from(DriverBid)
                .where(
                    DriverBid.load_id == load_id,
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.is_deleted.is_(False),
                )
            )
        else:
            is_driver_bid_vehicle = literal(False)

        # ----------------------------------------------------
        # Driver bid price
        # ----------------------------------------------------

        driver_bid_conditions = [
            DriverBid.vehicle_id == Vehicle.id,
            DriverBid.is_deleted.is_(False),
        ]
        if load_id:
            driver_bid_conditions.append(DriverBid.load_id == load_id)

        driver_bid_price = (
            select(DriverBid.driver_price)
            .where(*driver_bid_conditions)
            .order_by(DriverBid.id.desc())
            .limit(1)
            .scalar_subquery()
        )

        # ----------------------------------------------------
        # Owner bid
        # ----------------------------------------------------

        owner_conditions = [
            DriverBid.vehicle_id == Vehicle.id,
            DriverBid.owner_bid.is_(True),
            DriverBid.is_deleted.is_(False),
        ]
        if load_id:
            owner_conditions.append(DriverBid.load_id == load_id)

        owner_bid = exists(
            select(1).select_from(DriverBid).where(*owner_conditions)
        )

        # ----------------------------------------------------
        # Is on load
        # ----------------------------------------------------

        is_on_load = exists(
            select(1)
            .select_from(ConfirmedLoad)
            .where(
                ConfirmedLoad.vehicle_id == Vehicle.id,
                ConfirmedLoad.status.in_((1, 2, 3, 4)),
                ConfirmedLoad.is_deleted.is_(False),
            )
        )

        # ----------------------------------------------------
        # BID
        # ----------------------------------------------------

        if is_bid:
            return await self._bid_distance(
                lat=lat,
                lon=lon,
                radius=radius,
                base=base,
                team=team,
                vehicle_id=vehicle_id,
                is_driver_bid_vehicle=is_driver_bid_vehicle,
                driver_bid_price=driver_bid_price,
                owner_bid=owner_bid,
                is_on_load=is_on_load,
                params=params,
            )

        # ----------------------------------------------------
        # NORMAL
        # ----------------------------------------------------

        return await self._normal_distance(
            lat=lat,
            lon=lon,
            radius=radius,
            base=base,
            team=team,
            is_driver_bid_vehicle=is_driver_bid_vehicle,
            driver_bid_price=driver_bid_price,
            owner_bid=owner_bid,
            is_on_load=is_on_load,
            params=params,
        )

    # ========================================================
    # BID DISTANCE
    # ========================================================

    async def _bid_distance(
        self,
        *,
        lat: float,
        lon: float,
        radius: float,
        base,
        team,
        vehicle_id: int | None,
        is_driver_bid_vehicle,
        driver_bid_price,
        owner_bid,
        is_on_load,
        params: VehicleListParams,
    ):
        distance = _haversine(Vehicle.latitude, Vehicle.longitude, lat, lon)
        requested = (
            Vehicle.id == vehicle_id
            if vehicle_id is not None
            else literal(False)
        )
        selected = (
            Vehicle.id.in_(params.vehicle_ids)
            if params.vehicle_ids
            else literal(False)
        )

        bbox = _bbox(Vehicle.latitude, Vehicle.longitude, lat, lon, radius)

        where = and_(
            base,
            Vehicle.latitude.is_not(None),
            Vehicle.longitude.is_not(None),
            or_(
                requested,
                is_driver_bid_vehicle,
                and_(bbox, distance <= radius),
            ),
        )

        if team is not None:
            where = and_(where, team)

        stmt = select(
            Vehicle.id.label("vid"),
            distance.label("sky_distance"),
            literal("current").label("location_type"),
            requested.label("is_requested_vehicle"),
            is_driver_bid_vehicle.label("is_driver_bid_vehicle"),
            driver_bid_price.label("driver_bid_price"),
            owner_bid.label("owner_bid"),
            is_on_load.label("is_on_load"),
            selected.label("is_selected_vehicle"),
        ).where(where)

        order = []
        if params.vehicle_ids:
            order.append(selected.desc())

        order.extend(
            [
                requested.desc(),
                is_driver_bid_vehicle.desc(),
                distance.asc(),
                Vehicle.id.asc(),
            ]
        )

        stmt = stmt.order_by(*order)

        return await self._paginate_distance(stmt, params)

    # ========================================================
    # NORMAL DISTANCE
    # ========================================================

    async def _normal_distance(
        self,
        *,
        lat: float,
        lon: float,
        radius: float,
        base,
        team,
        is_driver_bid_vehicle,
        driver_bid_price,
        owner_bid,
        is_on_load,
        params: VehicleListParams,
    ):
        selected = (
            Vehicle.id.in_(params.vehicle_ids)
            if params.vehicle_ids
            else literal(False)
        )

        # ====================================================
        # CURRENT
        # ====================================================

        current_distance = _haversine(Vehicle.latitude, Vehicle.longitude, lat, lon)
        current_bbox = _bbox(Vehicle.latitude, Vehicle.longitude, lat, lon, radius)

        current_where = and_(
            base,
            Vehicle.latitude.is_not(None),
            Vehicle.longitude.is_not(None),
            or_(
                is_driver_bid_vehicle,
                and_(current_bbox, current_distance <= radius),
            ),
        )

        if team is not None:
            current_where = and_(current_where, team)

        current = select(
            Vehicle.id.label("vid"),
            current_distance.label("sky_distance"),
            literal("current").label("location_type"),
            literal(None).label("is_requested_vehicle"),
            is_driver_bid_vehicle.label("is_driver_bid_vehicle"),
            driver_bid_price.label("driver_bid_price"),
            owner_bid.label("owner_bid"),
            is_on_load.label("is_on_load"),
            selected.label("is_selected_vehicle"),
        ).where(current_where)

        # ====================================================
        # PLANNED
        # ====================================================

        planned_distance = _haversine(
            Vehicle.planned_latitude, Vehicle.planned_longitude, lat, lon
        )
        planned_bbox = _bbox(
            Vehicle.planned_latitude, Vehicle.planned_longitude, lat, lon, radius
        )

        planned_where = and_(
            base,
            Vehicle.planned_latitude.is_not(None),
            Vehicle.planned_longitude.is_not(None),
            Vehicle.planned_address.is_not(None),
            Vehicle.planned_address != "",
            or_(
                is_driver_bid_vehicle,
                and_(planned_bbox, planned_distance <= radius),
            ),
        )

        if team is not None:
            planned_where = and_(planned_where, team)

        planned = select(
            Vehicle.id.label("vid"),
            planned_distance.label("sky_distance"),
            literal("planned").label("location_type"),
            literal(None).label("is_requested_vehicle"),
            is_driver_bid_vehicle.label("is_driver_bid_vehicle"),
            driver_bid_price.label("driver_bid_price"),
            owner_bid.label("owner_bid"),
            is_on_load.label("is_on_load"),
            selected.label("is_selected_vehicle"),
        ).where(planned_where)

        # ====================================================
        # UNION + DEDUP с двойным CTE
        # ====================================================

        # Шаг 1: UNION current и planned
        unioned = union_all(current, planned).cte("vehicle_distance")

        # Шаг 2: Добавляем row_number()
        with_row_number = select(
            unioned.c.vid,
            unioned.c.sky_distance,
            unioned.c.location_type,
            unioned.c.is_requested_vehicle,
            unioned.c.is_driver_bid_vehicle,
            unioned.c.driver_bid_price,
            unioned.c.owner_bid,
            unioned.c.is_on_load,
            unioned.c.is_selected_vehicle,
            func.row_number()
            .over(
                partition_by=unioned.c.vid,
                order_by=unioned.c.sky_distance.asc(),
            )
            .label("rn"),
        ).cte("vehicle_with_rn")

        # Шаг 3: Фильтруем rn = 1
        stmt = (
            select(
                with_row_number.c.vid,
                with_row_number.c.sky_distance,
                with_row_number.c.location_type,
                with_row_number.c.is_requested_vehicle,
                with_row_number.c.is_driver_bid_vehicle,
                with_row_number.c.driver_bid_price,
                with_row_number.c.owner_bid,
                with_row_number.c.is_on_load,
                with_row_number.c.is_selected_vehicle,
            )
            .where(with_row_number.c.rn == 1)
            .order_by(
                with_row_number.c.is_selected_vehicle.desc(),
                with_row_number.c.is_driver_bid_vehicle.desc(),
                with_row_number.c.sky_distance.asc(),
                with_row_number.c.vid.asc(),
            )
        )

        return await self._paginate_distance(stmt, params)

    # ========================================================
    # PAGINATION
    # ========================================================

    async def _paginate_distance(
        self,
        stmt,
        params: VehicleListParams,
    ):
        offset = (params.page - 1) * params.page_size

        # ----------------------------------------------------
        # First get page IDs.
        # +1 lets us know if next page exists.
        # ----------------------------------------------------

        page_stmt = stmt.offset(offset).limit(params.page_size + 1)

        rows = (await self.session.execute(page_stmt)).mappings().all()

        if not rows:
            return 0, []

        has_more = len(rows) > params.page_size
        rows = rows[: params.page_size]

        vehicle_ids = list(dict.fromkeys(row["vid"] for row in rows))

        if not vehicle_ids:
            return 0, []

        # ----------------------------------------------------
        # Fetch actual vehicles only once.
        # ----------------------------------------------------

        vehicles = (
            await self.session.scalars(
                select(Vehicle)
                .where(Vehicle.id.in_(vehicle_ids))
                .options(selectinload(Vehicle.equipment))
            )
        ).unique().all()

        by_id = {vehicle.id: vehicle for vehicle in vehicles}

        results = []
        for row in rows:
            vehicle = by_id.get(row["vid"])
            if vehicle is None:
                continue

            results.append(
                VehicleSchema.from_vehicle(
                    vehicle,
                    sky_distance=row.get("sky_distance"),
                    location_type=row.get("location_type"),
                    driver_bid_price=row.get("driver_bid_price"),
                    owner_bid=row.get("owner_bid"),
                    is_on_load=row.get("is_on_load"),
                    is_requested_vehicle=row.get("is_requested_vehicle"),
                )
            )

        # ----------------------------------------------------
        # OPTIMIZED COUNT:
        #
        # Для geo-запросов не считаем точный total.
        # Это требует пересчета Haversine для всех строк.
        #
        # Возвращаем -1 как сигнал "неизвестно".
        # ----------------------------------------------------

        return -1, results