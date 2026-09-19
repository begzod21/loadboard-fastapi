from __future__ import annotations

from dataclasses import dataclass, field

from geoalchemy2.functions import ST_DWithin, ST_Distance
from sqlalchemy import (
    and_,
    case,
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


MILES_TO_METERS = 1609.344
DEFAULT_RADIUS_MILES = 300


@dataclass
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

    # ============================================================
    # PUBLIC
    # ============================================================

    async def list(
        self,
        params: VehicleListParams,
        filters: VehicleFilter,
    ) -> tuple[int, list[VehicleSchema]]:

        latitude = params.latitude
        longitude = params.longitude

        # --------------------------------------------------------
        # Address -> coordinates
        # --------------------------------------------------------

        if params.address:
            longitude, latitude = await self.map_service.get_coordinates(
                params.address
            )

            if longitude is None or latitude is None:
                return 0, []

        # --------------------------------------------------------
        # Load / Bid context
        # --------------------------------------------------------

        load: Load | None = None
        vehicle_id: int | None = None
        driver_bid_vehicle_ids: list[int] = []

        if params.load_id:
            load = await self.session.get(
                Load,
                params.load_id,
            )

            if load is None:
                raise LookupError(
                    f"Load not found! ID: {params.load_id}"
                )

            if (
                load.pick_up_longitude is not None
                and load.pick_up_latitude is not None
            ):
                longitude = float(load.pick_up_longitude)
                latitude = float(load.pick_up_latitude)

            driver_bid_vehicle_ids = (
                await self._driver_bid_vehicle_ids(params.load_id)
            )

        if params.bid_id:
            bid = await self.session.get(
                Bid,
                params.bid_id,
            )

            if bid is None:
                raise LookupError(
                    f"Bid not found! ID: {params.bid_id}"
                )

            if not bid.load_id:
                raise LookupError(
                    "This bid has no Load!"
                )

            load = await self.session.get(
                Load,
                bid.load_id,
            )

            if load is not None:
                if (
                    load.pick_up_longitude is not None
                    and load.pick_up_latitude is not None
                ):
                    longitude = float(load.pick_up_longitude)
                    latitude = float(load.pick_up_latitude)

            vehicle_id = bid.vehicle_id

        # --------------------------------------------------------
        # Matching filters
        # --------------------------------------------------------

        matching_vehicle_type: str | None = None
        matching_weight: int | None = None

        if params.has_matching_vehicles and load is not None:

            if load.vehicle_type:
                matching_vehicle_type = load.vehicle_type

            if load.weight is not None:
                matching_weight = load.weight

        # --------------------------------------------------------
        # Plain listing
        # --------------------------------------------------------

        if latitude is None or longitude is None:
            return await self._plain_list(
                filters=filters,
                params=params,
                matching_vehicle_type=matching_vehicle_type,
                matching_weight=matching_weight,
            )

        # --------------------------------------------------------
        # Proximity listing
        # --------------------------------------------------------

        radius = (
            DEFAULT_RADIUS_MILES
            if params.radius is None or params.radius == -1
            else params.radius
        )

        return await self._distance_list(
            lat=float(latitude),
            lon=float(longitude),
            radius=radius,
            vehicle_id=vehicle_id,
            driver_bid_vehicle_ids=driver_bid_vehicle_ids,
            is_bid=params.bid_id is not None,
            load_id=(
                params.load_id
                if params.load_id
                else (
                    load.id
                    if params.bid_id and load is not None
                    else None
                )
            ),
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

    # ============================================================
    # COMMON FILTER
    # ============================================================

    def _base_filter(
        self,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ):
        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        if (
            params.vehicle_ids
            and show_only_selected
        ):
            conditions.append(
                Vehicle.id.in_(params.vehicle_ids)
            )

        if matching_vehicle_type:
            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        if matching_weight is not None:
            conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        return and_(*conditions)

    # ============================================================
    # TEAM FILTER
    # ============================================================

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

    # ============================================================
    # PLAIN LIST
    # ============================================================

    async def _plain_list(
        self,
        filters: VehicleFilter,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        where = self._base_filter(
            params,
            matching_vehicle_type,
            matching_weight,
        )

        combined = filters.combined()

        if combined is not None:
            where = and_(
                where,
                combined,
            )

        count = await self.session.scalar(
            select(func.count(Vehicle.id))
            .where(where)
        )

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        order_by = []

        if (
            params.vehicle_ids
            and not show_only_selected
        ):
            order_by.append(
                case(
                    (
                        Vehicle.id.in_(params.vehicle_ids),
                        0,
                    ),
                    else_=1,
                )
            )

        order_by.append(
            Vehicle.id.desc()
        )

        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(*order_by)
            .offset(
                (params.page - 1) * params.page_size
            )
            .limit(params.page_size)
            .options(
                selectinload(Vehicle.equipment)
            )
        )

        vehicles = (
            await self.session.scalars(stmt)
        ).unique().all()

        return (
            int(count or 0),
            [
                VehicleSchema.from_vehicle(vehicle)
                for vehicle in vehicles
            ],
        )

    # ============================================================
    # DISTANCE LIST
    # ============================================================

    async def _distance_list(
        self,
        *,
        lat: float,
        lon: float,
        radius: float,
        vehicle_id: int | None,
        driver_bid_vehicle_ids: list[int],
        is_bid: bool,
        load_id: int | None,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        # --------------------------------------------------------
        # Geography point
        #
        # geography uses meters.
        # --------------------------------------------------------

        target_point = func.ST_SetSRID(
            func.ST_MakePoint(
                lon,
                lat,
            ),
            4326,
        )

        radius_meters = radius * MILES_TO_METERS

        base_filter = self._base_filter(
            params,
            matching_vehicle_type,
            matching_weight,
        )

        team_filter = self._team_filter(
            is_bid=is_bid,
            vehicle_id=vehicle_id,
        )

        # --------------------------------------------------------
        # Driver bid EXISTS
        #
        # IMPORTANT:
        # Do NOT build:
        #
        # Vehicle.id IN ([1000, 1001, ...])
        #
        # PostgreSQL can use this correlated EXISTS with index.
        # --------------------------------------------------------

        driver_bid_exists = literal(False)

        if load_id:

            driver_bid_exists = exists(
                select(1)
                .select_from(DriverBid)
                .where(
                    DriverBid.load_id == load_id,
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.vehicle_id.is_not(None),
                    DriverBid.is_deleted.is_(False),
                )
            )

        # --------------------------------------------------------
        # Driver bid price
        # --------------------------------------------------------

        driver_bid_price = None

        if load_id:

            driver_bid_price = (
                select(DriverBid.driver_price)
                .where(
                    DriverBid.load_id == load_id,
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.is_deleted.is_(False),
                )
                .order_by(
                    DriverBid.id.desc()
                )
                .limit(1)
                .scalar_subquery()
            )

        else:

            driver_bid_price = (
                select(DriverBid.driver_price)
                .where(
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.is_deleted.is_(False),
                )
                .order_by(
                    DriverBid.id.desc()
                )
                .limit(1)
                .scalar_subquery()
            )

        # --------------------------------------------------------
        # Owner bid
        # --------------------------------------------------------

        owner_bid = exists(
            select(1)
            .select_from(DriverBid)
            .where(
                DriverBid.vehicle_id == Vehicle.id,
                DriverBid.owner_bid.is_(True),
                DriverBid.is_deleted.is_(False),
                *(
                    [DriverBid.load_id == load_id]
                    if load_id
                    else []
                ),
            )
        )

        # --------------------------------------------------------
        # Is vehicle currently on load
        # --------------------------------------------------------

        is_on_load = exists(
            select(1)
            .select_from(ConfirmedLoad)
            .where(
                ConfirmedLoad.vehicle_id == Vehicle.id,
                ConfirmedLoad.status.in_(
                    [1, 2, 3, 4]
                ),
                ConfirmedLoad.is_deleted.is_(False),
            )
        )

        # ========================================================
        # BID MODE
        # ========================================================

        if is_bid:

            return await self._bid_distance_list(
                target_point=target_point,
                radius_meters=radius_meters,
                base_filter=base_filter,
                team_filter=team_filter,
                driver_bid_exists=driver_bid_exists,
                driver_bid_price=driver_bid_price,
                owner_bid=owner_bid,
                is_on_load=is_on_load,
                vehicle_id=vehicle_id,
                params=params,
            )

        # ========================================================
        # NORMAL MODE
        # ========================================================

        return await self._normal_distance_list(
            target_point=target_point,
            radius_meters=radius_meters,
            base_filter=base_filter,
            team_filter=team_filter,
            driver_bid_exists=driver_bid_exists,
            driver_bid_price=driver_bid_price,
            owner_bid=owner_bid,
            is_on_load=is_on_load,
            params=params,
        )

    # ============================================================
    # BID PROXIMITY
    # ============================================================

    async def _bid_distance_list(
        self,
        *,
        target_point,
        radius_meters: float,
        base_filter,
        team_filter,
        driver_bid_exists,
        driver_bid_price,
        owner_bid,
        is_on_load,
        vehicle_id: int | None,
        params: VehicleListParams,
    ):

        distance = ST_Distance(
            Vehicle.location,
            target_point,
        )

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

        # --------------------------------------------------------
        # Spatial filter.
        #
        # Requested vehicle and driver-bid vehicles are included
        # even if they are outside radius.
        # --------------------------------------------------------

        spatial_filter = ST_DWithin(
            Vehicle.location,
            target_point,
            radius_meters,
        )

        where = and_(
            base_filter,
            or_(
                requested,
                driver_bid_exists,
                spatial_filter,
            ),
        )

        if team_filter is not None:
            where = and_(
                where,
                team_filter,
            )

        stmt = select(
            Vehicle.id.label("vid"),
            distance.label("sky_distance"),
            literal("current").label(
                "location_type"
            ),
            requested.label(
                "is_requested_vehicle"
            ),
            driver_bid_exists.label(
                "is_driver_bid_vehicle"
            ),
            driver_bid_price.label(
                "driver_bid_price"
            ),
            owner_bid.label(
                "owner_bid"
            ),
            is_on_load.label(
                "is_on_load"
            ),
            selected.label(
                "is_selected_vehicle"
            ),
        ).where(where)

        order_by = []

        if params.vehicle_ids:
            order_by.append(
                selected.desc()
            )

        order_by.extend(
            [
                requested.desc(),
                driver_bid_exists.desc(),
                distance.asc(),
                Vehicle.id.asc(),
            ]
        )

        stmt = stmt.order_by(
            *order_by
        )

        return await self._materialise(
            stmt,
            params,
        )

    # ============================================================
    # NORMAL PROXIMITY
    # ============================================================

    async def _normal_distance_list(
        self,
        *,
        target_point,
        radius_meters: float,
        base_filter,
        team_filter,
        driver_bid_exists,
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

        # --------------------------------------------------------
        # CURRENT
        # --------------------------------------------------------

        current_distance = ST_Distance(
            Vehicle.location,
            target_point,
        )

        current_spatial = ST_DWithin(
            Vehicle.location,
            target_point,
            radius_meters,
        )

        current_where = and_(
            base_filter,
            or_(
                driver_bid_exists,
                current_spatial,
            ),
        )

        if team_filter is not None:
            current_where = and_(
                current_where,
                team_filter,
            )

        current = select(
            Vehicle.id.label("vid"),
            current_distance.label(
                "sky_distance"
            ),
            literal("current").label(
                "location_type"
            ),
            literal(None).label(
                "is_requested_vehicle"
            ),
            driver_bid_exists.label(
                "is_driver_bid_vehicle"
            ),
            driver_bid_price.label(
                "driver_bid_price"
            ),
            owner_bid.label(
                "owner_bid"
            ),
            is_on_load.label(
                "is_on_load"
            ),
            selected.label(
                "is_selected_vehicle"
            ),
        ).where(
            current_where
        )

        # --------------------------------------------------------
        # PLANNED
        # --------------------------------------------------------

        planned_distance = ST_Distance(
            Vehicle.planned_location,
            target_point,
        )

        planned_spatial = ST_DWithin(
            Vehicle.planned_location,
            target_point,
            radius_meters,
        )

        planned_where = and_(
            base_filter,
            Vehicle.planned_location.is_not(None),
            or_(
                driver_bid_exists,
                planned_spatial,
            ),
        )

        if team_filter is not None:
            planned_where = and_(
                planned_where,
                team_filter,
            )

        planned = select(
            Vehicle.id.label("vid"),
            planned_distance.label(
                "sky_distance"
            ),
            literal("planned").label(
                "location_type"
            ),
            literal(None).label(
                "is_requested_vehicle"
            ),
            driver_bid_exists.label(
                "is_driver_bid_vehicle"
            ),
            driver_bid_price.label(
                "driver_bid_price"
            ),
            owner_bid.label(
                "owner_bid"
            ),
            is_on_load.label(
                "is_on_load"
            ),
            selected.label(
                "is_selected_vehicle"
            ),
        ).where(
            planned_where
        )

        # --------------------------------------------------------
        # UNION
        # --------------------------------------------------------

        unioned = union_all(
            current,
            planned,
        ).subquery(
            "vehicle_distance"
        )

        stmt = (
            select(unioned)
            .order_by(
                unioned.c.is_selected_vehicle.desc(),
                unioned.c.is_driver_bid_vehicle.desc(),
                unioned.c.sky_distance.asc(),
                unioned.c.vid.asc(),
            )
        )

        return await self._materialise(
            stmt,
            params,
        )

    # ============================================================
    # MATERIALISE
    # ============================================================

    async def _materialise(
        self,
        stmt,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:

        # --------------------------------------------------------
        # We intentionally DON'T use COUNT() OVER().
        #
        # Fetch one extra row to determine has_next.
        # --------------------------------------------------------

        offset = (
            (params.page - 1)
            * params.page_size
        )

        page_stmt = (
            stmt
            .offset(offset)
            .limit(params.page_size + 1)
        )

        rows = (
            await self.session.execute(
                page_stmt
            )
        ).mappings().all()

        if not rows:
            return 0, []

        has_next = (
            len(rows) > params.page_size
        )

        rows = rows[:params.page_size]

        vehicle_ids = list(
            dict.fromkeys(
                row["vid"]
                for row in rows
            )
        )

        if not vehicle_ids:
            return 0, []

        # --------------------------------------------------------
        # Fetch actual Vehicle objects ONLY for current page.
        # --------------------------------------------------------

        vehicles = (
            await self.session.scalars(
                select(Vehicle)
                .where(
                    Vehicle.id.in_(vehicle_ids)
                )
                .options(
                    selectinload(
                        Vehicle.equipment
                    )
                )
            )
        ).unique().all()

        by_id = {
            vehicle.id: vehicle
            for vehicle in vehicles
        }

        results = []

        for row in rows:

            vehicle = by_id.get(
                row["vid"]
            )

            if vehicle is None:
                continue

            results.append(
                VehicleSchema.from_vehicle(
                    vehicle,
                    sky_distance=row.get(
                        "sky_distance"
                    ),
                    location_type=row.get(
                        "location_type"
                    ),
                    driver_bid_price=row.get(
                        "driver_bid_price"
                    ),
                    owner_bid=row.get(
                        "owner_bid"
                    ),
                    is_on_load=row.get(
                        "is_on_load"
                    ),
                    is_requested_vehicle=row.get(
                        "is_requested_vehicle"
                    ),
                )
            )

        # --------------------------------------------------------
        # API currently expects total count.
        #
        # We don't have COUNT OVER anymore.
        #
        # Returning page-based count would break existing API.
        #
        # Therefore count only when necessary.
        # --------------------------------------------------------

        total = await self._count_distance_rows(
            stmt
        )

        return total, results

    # ============================================================
    # COUNT
    # ============================================================

    async def _count_distance_rows(
        self,
        stmt,
    ) -> int:

        count_stmt = select(
            func.count()
        ).select_from(
            stmt.order_by(None).subquery()
        )

        count = await self.session.scalar(
            count_stmt
        )

        return int(count or 0)

    # ============================================================
    # DRIVER BID VEHICLES
    # ============================================================

    async def _driver_bid_vehicle_ids(
        self,
        load_id: int,
    ) -> list[int]:

        stmt = (
            select(
                DriverBid.vehicle_id
            )
            .where(
                DriverBid.load_id == load_id,
                DriverBid.vehicle_id.is_not(None),
                DriverBid.is_deleted.is_(False),
            )
        )

        if self.team_ids:

            stmt = (
                stmt.join(
                    Vehicle,
                    Vehicle.id
                    == DriverBid.vehicle_id,
                )
                .where(
                    or_(
                        Vehicle.team_id.in_(
                            self.team_ids
                        ),
                        Vehicle.team_id.is_(None),
                    )
                )
            )

        result = await self.session.scalars(
            stmt
        )

        return list(
            dict.fromkeys(
                vehicle_id
                for vehicle_id in result.all()
                if vehicle_id is not None
            )
        )