from __future__ import annotations

from dataclasses import dataclass, field

from geoalchemy2 import Geography
from sqlalchemy import (
    and_,
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


MILES_TO_METERS = 1609.344
DEFAULT_RADIUS_MILES = 300.0


@dataclass
class VehicleListParams:
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    radius: float | None = None
    load_id: int | None = None
    bid_id: int | None = None
    vehicle_ids: list[int] = field(default_factory=list)
    has_matching_vehicles: bool = False
    page: int = 1
    page_size: int = 20


class VehicleListService:
    """
    Vehicle search optimized for PostgreSQL/PostGIS.

    Distance search uses:
        Vehicle.location          -> Geography(Point, 4326)
        Vehicle.planned_location  -> Geography(Point, 4326)

    Radius filtering uses ST_DWithin(), so PostgreSQL can use GiST indexes.
    Exact distance is calculated only for rows that passed ST_DWithin().
    """

    def __init__(
        self,
        session: AsyncSession,
        user: CurrentUser,
        mapbox_token: str | None = None,
    ) -> None:
        self.session = session
        self.team_ids = user.team_ids
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
            longitude, latitude = (
                await self.map_service.get_coordinates(
                    params.address
                )
            )

            if longitude is None or latitude is None:
                return 0, []

        vehicle_id: int | None = None
        load: Load | None = None
        bid: Bid | None = None

        # --------------------------------------------------------
        # Load
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # Bid
        # --------------------------------------------------------

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

            if (
                load is not None
                and load.pick_up_longitude is not None
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
        # Distance search
        # --------------------------------------------------------

        if latitude is not None and longitude is not None:
            load_id = (
                params.load_id
                if params.load_id
                else bid.load_id
                if bid is not None
                else None
            )

            radius = (
                DEFAULT_RADIUS_MILES
                if params.radius == -1
                or params.radius is None
                else params.radius
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

        return await self._plain_list(
            filters=filters,
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

    # ============================================================
    # COMMON VEHICLE CONDITIONS
    # ============================================================

    def _base_conditions(
        self,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ) -> list:
        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        if params.vehicle_ids:
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

        return conditions

    def _team_condition(
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

        # Preserve original bid behaviour: requested vehicle
        # remains visible even if it is outside the team scope.
        if is_bid and vehicle_id is not None:
            condition = or_(
                condition,
                Vehicle.id == vehicle_id,
            )

        return condition

    # ============================================================
    # NORMAL LIST
    # ============================================================

    async def _plain_list(
        self,
        filters: VehicleFilter,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        conditions = self._base_conditions(
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

        combined = filters.combined()

        if combined is not None:
            conditions.append(combined)

        where = and_(*conditions)

        count = await self.session.scalar(
            select(func.count(Vehicle.id))
            .where(where)
        )

        offset = max(params.page - 1, 0) * params.page_size

        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(Vehicle.id.desc())
            .offset(offset)
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
    # POSTGIS HELPERS
    # ============================================================

    @staticmethod
    def _point(
        lat: float,
        lon: float,
    ):
        """
        Geography(Point, 4326).

        Keep the search point as geography so ST_DWithin() and
        ST_Distance() operate in meters.
        """
        point = func.ST_SetSRID(
            func.ST_MakePoint(lon, lat),
            4326,
        )

        return cast(
            point,
            Geography(
                geometry_type="POINT",
                srid=4326,
            ),
        )

    @staticmethod
    def _distance_miles(
        column,
        point,
    ):
        return (
            func.ST_Distance(
                column,
                point,
            )
            / MILES_TO_METERS
        )

    @staticmethod
    def _within_radius(
        column,
        point,
        radius_miles: float,
    ):
        return func.ST_DWithin(
            column,
            point,
            radius_miles * MILES_TO_METERS,
        )

    # ============================================================
    # DISTANCE LIST
    # ============================================================

    async def _distance_list(
        self,
        lat: float,
        lon: float,
        radius: float,
        vehicle_id: int | None,
        is_bid: bool,
        load_id: int | None,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        point = self._point(lat, lon)

        base_conditions = self._base_conditions(
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

        team_condition = self._team_condition(
            is_bid=is_bid,
            vehicle_id=vehicle_id,
        )

        # --------------------------------------------------------
        # DriverBid EXISTS
        #
        # Replaces the old:
        #   SELECT vehicle IDs
        #   -> Python list
        #   -> WHERE id IN (...)
        #
        # This avoids transferring potentially thousands of IDs
        # from PostgreSQL to Python.
        # --------------------------------------------------------

        if load_id is not None:
            driver_bid_exists = exists(
                select(DriverBid.id)
                .where(
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.load_id == load_id,
                    DriverBid.is_deleted.is_(False),
                )
            )

            driver_bid_price = (
                select(DriverBid.driver_price)
                .where(
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.load_id == load_id,
                    DriverBid.is_deleted.is_(False),
                )
                .order_by(DriverBid.id.desc())
                .limit(1)
                .scalar_subquery()
            )

            owner_bid = exists(
                select(DriverBid.id)
                .where(
                    DriverBid.vehicle_id == Vehicle.id,
                    DriverBid.load_id == load_id,
                    DriverBid.owner_bid.is_(True),
                    DriverBid.is_deleted.is_(False),
                )
            )
        else:
            driver_bid_exists = literal(False)
            driver_bid_price = literal(None)
            owner_bid = literal(False)

        # --------------------------------------------------------
        # ConfirmedLoad EXISTS
        # --------------------------------------------------------

        is_on_load = exists(
            select(ConfirmedLoad.id)
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
                point=point,
                radius=radius,
                vehicle_id=vehicle_id,
                base_conditions=base_conditions,
                team_condition=team_condition,
                driver_bid_exists=driver_bid_exists,
                driver_bid_price=driver_bid_price,
                owner_bid=owner_bid,
                is_on_load=is_on_load,
                params=params,
            )

        # ========================================================
        # NORMAL CURRENT + PLANNED
        # ========================================================

        return await self._current_and_planned_list(
            point=point,
            radius=radius,
            base_conditions=base_conditions,
            team_condition=team_condition,
            driver_bid_exists=driver_bid_exists,
            driver_bid_price=driver_bid_price,
            owner_bid=owner_bid,
            is_on_load=is_on_load,
            params=params,
        )

    # ============================================================
    # BID DISTANCE
    # ============================================================

    async def _bid_distance_list(
        self,
        *,
        point,
        radius: float,
        vehicle_id: int | None,
        base_conditions: list,
        team_condition,
        driver_bid_exists,
        driver_bid_price,
        owner_bid,
        is_on_load,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:

        distance = self._distance_miles(
            Vehicle.location,
            point,
        )

        is_requested = (
            Vehicle.id == vehicle_id
            if vehicle_id is not None
            else literal(False)
        )

        conditions = [
            *base_conditions,
            Vehicle.location.is_not(None),
        ]

        if team_condition is not None:
            conditions.append(team_condition)

        # --------------------------------------------------------
        # GiST-backed spatial predicate.
        #
        # PostgreSQL can use:
        #   idx_vehicle_location_gist
        # --------------------------------------------------------

        conditions.append(
            or_(
                is_requested,
                driver_bid_exists,
                self._within_radius(
                    Vehicle.location,
                    point,
                    radius,
                ),
            )
        )

        stmt = (
            select(
                Vehicle.id.label("vid"),
                distance.label("sky_distance"),
                literal("current").label("location_type"),
                is_requested.label(
                    "is_requested_vehicle"
                ),
                driver_bid_exists.label(
                    "is_driver_bid_vehicle"
                ),
                driver_bid_price.label(
                    "driver_bid_price"
                ),
                owner_bid.label("owner_bid"),
                is_on_load.label("is_on_load"),
            )
            .where(*conditions)
            .order_by(
                is_requested.desc(),
                driver_bid_exists.desc(),
                distance.asc(),
                Vehicle.id.asc(),
            )
        )

        return await self._materialise(
            stmt,
            params,
        )

    # ============================================================
    # CURRENT + PLANNED
    # ============================================================

    async def _current_and_planned_list(
        self,
        *,
        point,
        radius: float,
        base_conditions: list,
        team_condition,
        driver_bid_exists,
        driver_bid_price,
        owner_bid,
        is_on_load,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:

        # --------------------------------------------------------
        # CURRENT
        # --------------------------------------------------------

        current_conditions = [
            *base_conditions,
            Vehicle.location.is_not(None),
        ]

        if team_condition is not None:
            current_conditions.append(
                team_condition
            )

        current_conditions.append(
            or_(
                driver_bid_exists,
                self._within_radius(
                    Vehicle.location,
                    point,
                    radius,
                ),
            )
        )

        current_distance = self._distance_miles(
            Vehicle.location,
            point,
        )

        current = select(
            Vehicle.id.label("vid"),
            current_distance.label("sky_distance"),
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
            owner_bid.label("owner_bid"),
            is_on_load.label("is_on_load"),
        ).where(
            *current_conditions
        )

        # --------------------------------------------------------
        # PLANNED
        # --------------------------------------------------------

        planned_conditions = [
            *base_conditions,
            Vehicle.planned_location.is_not(None),
            Vehicle.planned_address.is_not(None),
            Vehicle.planned_address != "",
        ]

        if team_condition is not None:
            planned_conditions.append(
                team_condition
            )

        planned_conditions.append(
            or_(
                driver_bid_exists,
                self._within_radius(
                    Vehicle.planned_location,
                    point,
                    radius,
                ),
            )
        )

        planned_distance = self._distance_miles(
            Vehicle.planned_location,
            point,
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
            owner_bid.label("owner_bid"),
            is_on_load.label("is_on_load"),
        ).where(
            *planned_conditions
        )

        # --------------------------------------------------------
        # UNION
        # --------------------------------------------------------

        unioned = union_all(
            current,
            planned,
        ).subquery("vehicle_locations")

        ordered = (
            select(unioned)
            .order_by(
                unioned.c.is_driver_bid_vehicle.desc(),
                unioned.c.sky_distance.asc(),
                unioned.c.vid.asc(),
            )
        )

        return await self._materialise(
            ordered,
            params,
        )

    # ============================================================
    # MATERIALISE
    # ============================================================

    async def _materialise(
        self,
        ordered_stmt,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:

        offset = max(params.page - 1, 0) * params.page_size

        # --------------------------------------------------------
        # COUNT
        #
        # No ORDER BY is needed for COUNT.
        # Removing it avoids unnecessary sorting work.
        # --------------------------------------------------------

        count_source = ordered_stmt.order_by(None).subquery()

        count = await self.session.scalar(
            select(func.count())
            .select_from(count_source)
        )

        # --------------------------------------------------------
        # PAGE
        # --------------------------------------------------------

        page_stmt = (
            ordered_stmt
            .offset(offset)
            .limit(params.page_size)
        )

        rows = (
            await self.session.execute(page_stmt)
        ).mappings().all()

        if not rows:
            return int(count or 0), []

        vehicle_ids = list(
            dict.fromkeys(
                row["vid"]
                for row in rows
            )
        )

        # --------------------------------------------------------
        # Fetch only the page's vehicles.
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

        results: list[VehicleSchema] = []

        for row in rows:
            vehicle = by_id.get(row["vid"])

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

        return int(count or 0), results
