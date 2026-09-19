from __future__ import annotations

from dataclasses import dataclass, field

from geoalchemy2 import Geography
from sqlalchemy import (
    and_,
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


# 1 mile = 1609.344 meters
MILES_TO_METERS = 1609.344


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

    def __init__(
        self,
        session: AsyncSession,
        user: CurrentUser,
        mapbox_token: str | None = None,
    ) -> None:
        self.session = session
        self.user = user
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
            longitude, latitude = await self.map_service.get_coordinates(
                params.address
            )

            if longitude is None or latitude is None:
                return 0, []

        # --------------------------------------------------------
        # Load
        # --------------------------------------------------------

        vehicle_id: int | None = None
        load: Load | None = None

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
                longitude = float(
                    load.pick_up_longitude
                )
                latitude = float(
                    load.pick_up_latitude
                )

        # --------------------------------------------------------
        # Bid
        # --------------------------------------------------------

        bid: Bid | None = None

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
                load
                and load.pick_up_longitude is not None
                and load.pick_up_latitude is not None
            ):
                longitude = float(
                    load.pick_up_longitude
                )
                latitude = float(
                    load.pick_up_latitude
                )

            vehicle_id = bid.vehicle_id

        # --------------------------------------------------------
        # Matching vehicle requirements
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

            radius = (
                params.radius
                if params.radius is not None
                else -1
            )

            load_id = None

            if params.load_id:
                load_id = params.load_id

            elif params.bid_id and bid:
                load_id = bid.load_id

            return await self._distance_list(
                lat=float(latitude),
                lon=float(longitude),
                radius=radius,
                vehicle_id=vehicle_id,
                is_bid=bool(params.bid_id),
                load_id=load_id,
                params=params,
                matching_vehicle_type=matching_vehicle_type,
                matching_weight=matching_weight,
            )

        # --------------------------------------------------------
        # Normal listing
        # --------------------------------------------------------

        return await self._plain_list(
            filters=filters,
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

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

        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        # --------------------------------------------------------
        # Vehicle IDs
        # --------------------------------------------------------

        if params.vehicle_ids:
            conditions.append(
                Vehicle.id.in_(params.vehicle_ids)
            )

        # --------------------------------------------------------
        # Vehicle type
        # --------------------------------------------------------

        if matching_vehicle_type:

            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------
        # Weight
        # --------------------------------------------------------

        if matching_weight is not None:

            conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # --------------------------------------------------------
        # Filters
        # --------------------------------------------------------

        combined = filters.combined()

        if combined is not None:
            conditions.append(combined)

        where = and_(*conditions)

        # --------------------------------------------------------
        # COUNT
        # --------------------------------------------------------

        count = await self.session.scalar(
            select(
                func.count(Vehicle.id)
            ).where(where)
        )

        # --------------------------------------------------------
        # PAGE
        # --------------------------------------------------------

        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(Vehicle.id.desc())
            .offset(
                (params.page - 1)
                * params.page_size
            )
            .limit(params.page_size)
            .options(
                selectinload(Vehicle.equipment)
            )
        )

        vehicles = (
            await self.session.scalars(stmt)
        ).unique().all()

        results = [
            VehicleSchema.from_vehicle(vehicle)
            for vehicle in vehicles
        ]

        return int(count or 0), results

    # ============================================================
    # DISTANCE SEARCH
    # ============================================================

    async def _distance_list(
        self,
        lat: float,
        lon: float,
        radius: float | None,
        vehicle_id: int | None,
        is_bid: bool,
        load_id: int | None,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        # --------------------------------------------------------
        # Default radius
        # --------------------------------------------------------

        effective_radius_miles = (
            300
            if radius == -1
            else radius
        )

        # --------------------------------------------------------
        # Point
        #
        # Geography(Point, 4326)
        # ST_DWithin radius = meters
        # --------------------------------------------------------

        search_point = func.ST_SetSRID(
            func.ST_MakePoint(
                lon,
                lat,
            ),
            4326,
        )

        # Cast search point to geography.
        #
        # IMPORTANT:
        # Vehicle.location itself should already be Geography.
        #

        search_point = func.cast(
            search_point,
            Geography(
                geometry_type="POINT",
                srid=4326,
            ),
        )

        radius_meters = (
            effective_radius_miles
            * MILES_TO_METERS
            if effective_radius_miles is not None
            else None
        )

        # ========================================================
        # BASE CONDITIONS
        # ========================================================

        base_conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        # --------------------------------------------------------
        # Vehicle IDs
        # --------------------------------------------------------

        if params.vehicle_ids:

            base_conditions.append(
                Vehicle.id.in_(params.vehicle_ids)
            )

        # --------------------------------------------------------
        # Vehicle type
        # --------------------------------------------------------

        if matching_vehicle_type:

            base_conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------
        # Weight
        # --------------------------------------------------------

        if matching_weight is not None:

            base_conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # ========================================================
        # TEAM
        # ========================================================

        team_condition = None

        if self.team_ids:

            team_condition = or_(
                Vehicle.team_id.in_(self.team_ids),
                Vehicle.team_id.is_(None),
            )

            if is_bid and vehicle_id:

                team_condition = or_(
                    team_condition,
                    Vehicle.id == vehicle_id,
                )

        # ========================================================
        # DRIVER BID
        # ========================================================

        if load_id:

            driver_bid_exists = exists(
                select(DriverBid.id)
                .where(
                    DriverBid.vehicle_id
                    == Vehicle.id,

                    DriverBid.load_id
                    == load_id,

                    DriverBid.is_deleted
                    .is_(False),
                )
            )

            driver_bid_price = (
                select(
                    DriverBid.driver_price
                )
                .where(
                    DriverBid.vehicle_id
                    == Vehicle.id,

                    DriverBid.load_id
                    == load_id,

                    DriverBid.is_deleted
                    .is_(False),
                )
                .order_by(
                    DriverBid.id.desc()
                )
                .limit(1)
                .scalar_subquery()
            )

            owner_bid = exists(
                select(DriverBid.id)
                .where(
                    DriverBid.vehicle_id
                    == Vehicle.id,

                    DriverBid.load_id
                    == load_id,

                    DriverBid.owner_bid
                    .is_(True),

                    DriverBid.is_deleted
                    .is_(False),
                )
            )

        else:

            driver_bid_exists = literal(False)

            driver_bid_price = literal(None)

            owner_bid = literal(False)

        # ========================================================
        # CONFIRMED LOAD
        # ========================================================

        is_on_load = exists(
            select(ConfirmedLoad.id)
            .where(
                ConfirmedLoad.vehicle_id
                == Vehicle.id,

                ConfirmedLoad.status.in_(
                    [1, 2, 3, 4]
                ),

                ConfirmedLoad.is_deleted
                .is_(False),
            )
        )

        # ========================================================
        # CURRENT DISTANCE
        # ========================================================

        current_distance = func.ST_Distance(
            Vehicle.location,
            search_point,
        ) / MILES_TO_METERS

        # ========================================================
        # BID MODE
        #
        # Only current location is needed.
        # ========================================================

        if is_bid:

            is_requested_vehicle = (
                Vehicle.id == vehicle_id
                if vehicle_id is not None
                else literal(False)
            )

            current_conditions = list(
                base_conditions
            )

            # ----------------------------------------------------
            # Location must exist
            # ----------------------------------------------------

            current_conditions.extend(
                [
                    Vehicle.location.is_not(None),
                ]
            )

            # ----------------------------------------------------
            # Team
            # ----------------------------------------------------

            if team_condition is not None:

                current_conditions.append(
                    team_condition
                )

            # ----------------------------------------------------
            # Spatial index filter
            #
            # THIS is the important part.
            #
            # ST_DWithin can use GiST index.
            # ----------------------------------------------------

            if radius_meters is not None:

                spatial_condition = (
                    func.ST_DWithin(
                        Vehicle.location,
                        search_point,
                        radius_meters,
                    )
                )

                current_conditions.append(
                    or_(
                        is_requested_vehicle,
                        driver_bid_exists,
                        spatial_condition,
                    )
                )

            stmt = (
                select(
                    Vehicle.id.label("vid"),

                    current_distance.label(
                        "sky_distance"
                    ),

                    literal(
                        "current"
                    ).label(
                        "location_type"
                    ),

                    is_requested_vehicle.label(
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
                )
                .where(
                    *current_conditions
                )
                .order_by(
                    is_requested_vehicle.desc(),
                    driver_bid_exists.desc(),
                    current_distance.asc(),
                )
            )

            return await self._materialise(
                stmt,
                params,
            )

        # ========================================================
        # NORMAL MODE
        #
        # CURRENT + PLANNED
        # ========================================================

        # --------------------------------------------------------
        # CURRENT
        # --------------------------------------------------------

        current_conditions = list(
            base_conditions
        )

        current_conditions.extend(
            [
                Vehicle.location.is_not(None),
            ]
        )

        if team_condition is not None:

            current_conditions.append(
                team_condition
            )

        if radius_meters is not None:

            current_conditions.append(
                or_(
                    driver_bid_exists,
                    func.ST_DWithin(
                        Vehicle.location,
                        search_point,
                        radius_meters,
                    ),
                )
            )

        current_stmt = select(
            Vehicle.id.label("vid"),

            current_distance.label(
                "sky_distance"
            ),

            literal(
                "current"
            ).label(
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

        ).where(
            *current_conditions
        )

        # ========================================================
        # PLANNED
        # ========================================================

        planned_conditions = list(
            base_conditions
        )

        planned_conditions.extend(
            [
                Vehicle.planned_location
                .is_not(None),

                Vehicle.planned_address
                .is_not(None),

                Vehicle.planned_address
                != "",
            ]
        )

        if team_condition is not None:

            planned_conditions.append(
                team_condition
            )

        planned_distance = func.ST_Distance(
            Vehicle.planned_location,
            search_point,
        ) / MILES_TO_METERS

        if radius_meters is not None:

            planned_conditions.append(
                or_(
                    driver_bid_exists,

                    func.ST_DWithin(
                        Vehicle.planned_location,
                        search_point,
                        radius_meters,
                    ),
                )
            )

        planned_stmt = select(
            Vehicle.id.label("vid"),

            planned_distance.label(
                "sky_distance"
            ),

            literal(
                "planned"
            ).label(
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

        ).where(
            *planned_conditions
        )

        # ========================================================
        # UNION
        # ========================================================

        unioned = union_all(
            current_stmt,
            planned_stmt,
        ).subquery("veh")

        ordered = (
            select(unioned)
            .order_by(
                unioned.c.is_driver_bid_vehicle.desc(),
                unioned.c.sky_distance.asc(),
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
    ):

        # --------------------------------------------------------
        # COUNT
        #
        # Keep current API behaviour.
        # --------------------------------------------------------

        count_stmt = select(
            func.count()
        ).select_from(
            ordered_stmt.subquery()
        )

        count = await self.session.scalar(
            count_stmt
        )

        # --------------------------------------------------------
        # PAGE
        # --------------------------------------------------------

        page_stmt = (
            ordered_stmt
            .offset(
                (params.page - 1)
                * params.page_size
            )
            .limit(
                params.page_size
            )
        )

        rows = (
            await self.session.execute(
                page_stmt
            )
        ).mappings().all()

        if not rows:
            return int(count or 0), []

        # --------------------------------------------------------
        # Vehicle IDs
        # --------------------------------------------------------

        vids = [
            row["vid"]
            for row in rows
        ]

        # --------------------------------------------------------
        # Load vehicles
        # --------------------------------------------------------

        vehicles = (
            await self.session.scalars(
                select(Vehicle)
                .where(
                    Vehicle.id.in_(vids)
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

        # --------------------------------------------------------
        # Serialize
        # --------------------------------------------------------

        results: list[VehicleSchema] = []

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

        return int(count or 0), results