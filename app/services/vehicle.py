from __future__ import annotations

from dataclasses import dataclass, field

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
        self.team_ids = user.team_ids

        self.map_service = MapService(mapbox_token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list(
        self,
        params: VehicleListParams,
        filters: VehicleFilter,
    ) -> tuple[int, list[VehicleSchema]]:

        latitude = params.latitude
        longitude = params.longitude

        # --------------------------------------------------------------
        # Address -> coordinates
        # --------------------------------------------------------------

        if params.address:
            longitude, latitude = await self.map_service.get_coordinates(
                params.address
            )

            if longitude is None or latitude is None:
                return 0, []

        # --------------------------------------------------------------
        # Load
        # --------------------------------------------------------------

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
                longitude = float(load.pick_up_longitude)
                latitude = float(load.pick_up_latitude)

        # --------------------------------------------------------------
        # Bid
        # --------------------------------------------------------------

        vehicle_id: int | None = None
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

            if load is not None:
                if (
                    load.pick_up_longitude is not None
                    and load.pick_up_latitude is not None
                ):
                    longitude = float(load.pick_up_longitude)
                    latitude = float(load.pick_up_latitude)

            vehicle_id = bid.vehicle_id

        # --------------------------------------------------------------
        # Matching vehicle filters
        # --------------------------------------------------------------

        matching_vehicle_type: str | None = None
        matching_weight: int | None = None

        if params.has_matching_vehicles and load is not None:

            if load.vehicle_type:
                matching_vehicle_type = load.vehicle_type

            if load.weight is not None:
                matching_weight = load.weight

        # --------------------------------------------------------------
        # Distance search
        # --------------------------------------------------------------

        if latitude is not None and longitude is not None:

            if params.bid_id and not params.load_id:
                load_id = bid.load_id if bid else None
            else:
                load_id = params.load_id

            return await self._distance_list(
                lat=float(latitude),
                lon=float(longitude),
                radius=params.radius,
                vehicle_id=vehicle_id,
                is_bid=bool(params.bid_id),
                load_id=load_id,
                params=params,
                matching_vehicle_type=matching_vehicle_type,
                matching_weight=matching_weight,
            )

        # --------------------------------------------------------------
        # Normal list
        # --------------------------------------------------------------

        return await self._plain_list(
            filters=filters,
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

    # ------------------------------------------------------------------
    # Plain list
    # ------------------------------------------------------------------

    async def _plain_list(
        self,
        filters: VehicleFilter,
        params: VehicleListParams,
        matching_vehicle_type: str | None = None,
        matching_weight: int | None = None,
    ) -> tuple[int, list[VehicleSchema]]:

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        # --------------------------------------------------------------
        # Selected vehicles
        # --------------------------------------------------------------

        if params.vehicle_ids and show_only_selected:

            conditions.append(
                Vehicle.id.in_(params.vehicle_ids)
            )

        # --------------------------------------------------------------
        # Vehicle type
        # --------------------------------------------------------------

        if matching_vehicle_type:

            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------------
        # Weight
        # --------------------------------------------------------------

        if matching_weight is not None:

            conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # --------------------------------------------------------------
        # User filters
        # --------------------------------------------------------------

        combined = filters.combined()

        if combined is not None:
            conditions.append(combined)

        where = and_(*conditions)

        # --------------------------------------------------------------
        # Count
        # --------------------------------------------------------------

        count_stmt = (
            select(func.count())
            .select_from(Vehicle)
            .where(where)
        )

        count = await self.session.scalar(count_stmt)

        # --------------------------------------------------------------
        # Ordering
        # --------------------------------------------------------------

        ordering = []

        if params.vehicle_ids and not show_only_selected:

            ordering.append(
                case(
                    (
                        Vehicle.id.in_(params.vehicle_ids),
                        0,
                    ),
                    else_=1,
                ).asc()
            )

        ordering.append(
            Vehicle.id.desc()
        )

        # --------------------------------------------------------------
        # Page
        # --------------------------------------------------------------

        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(*ordering)
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

        results = [
            VehicleSchema.from_vehicle(vehicle)
            for vehicle in vehicles
        ]

        return int(count or 0), results

    # ------------------------------------------------------------------
    # Geography helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _target_point(
        lat: float,
        lon: float,
    ):
        """
        Geography(Point, 4326).
        """

        return func.ST_SetSRID(
            func.ST_MakePoint(
                lon,
                lat,
            ),
            4326,
        ).cast(
            # Geography(Point, 4326)
            # Using the same type as Vehicle.location.
            Vehicle.location.type
        )

    # ------------------------------------------------------------------
    # Base filters
    # ------------------------------------------------------------------

    def _base_vehicle_conditions(
        self,
        params: VehicleListParams,
        vehicle_id: int | None,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
        driver_bid_exists=None,
    ):
        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        # --------------------------------------------------------------
        # Selected
        # --------------------------------------------------------------

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        if params.vehicle_ids and show_only_selected:

            selected_condition = Vehicle.id.in_(
                params.vehicle_ids
            )

            if driver_bid_exists is not None:

                selected_condition = or_(
                    selected_condition,
                    driver_bid_exists,
                )

            conditions.append(selected_condition)

        # --------------------------------------------------------------
        # Requested vehicle from bid
        # --------------------------------------------------------------

        if vehicle_id is not None:

            conditions.append(
                or_(
                    Vehicle.id == vehicle_id,
                    *conditions,
                )
            )

        # --------------------------------------------------------------
        # Vehicle type
        # --------------------------------------------------------------

        if matching_vehicle_type:

            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------------
        # Weight
        # --------------------------------------------------------------

        if matching_weight is not None:

            conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        return conditions

    # ------------------------------------------------------------------
    # Team filter
    # ------------------------------------------------------------------

    def _team_condition(
        self,
        is_bid: bool,
        vehicle_id: int | None,
    ):

        if not self.team_ids:
            return None

        condition = or_(
            Vehicle.team_id.in_(self.team_ids),
            Vehicle.team_id.is_(None),
        )

        if is_bid and vehicle_id is not None:

            condition = or_(
                condition,
                Vehicle.id == vehicle_id,
            )

        return condition

    # ------------------------------------------------------------------
    # Distance list
    # ------------------------------------------------------------------

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

        # --------------------------------------------------------------
        # Django behaviour:
        #
        # radius=-1 / missing -> 300 miles
        # --------------------------------------------------------------

        effective_radius = (
            300
            if radius is None or radius == -1
            else radius
        )

        radius_meters = (
            effective_radius * MILES_TO_METERS
            if effective_radius is not None
            else None
        )

        # --------------------------------------------------------------
        # Target geography point
        # --------------------------------------------------------------

        target = self._target_point(
            lat=lat,
            lon=lon,
        )

        # --------------------------------------------------------------
        # DriverBid EXISTS
        #
        # No Python list.
        # No IN (...)
        # --------------------------------------------------------------

        driver_bid_conditions = [
            DriverBid.vehicle_id == Vehicle.id,
            DriverBid.is_deleted.is_(False),
        ]

        if load_id is not None:
            driver_bid_conditions.append(
                DriverBid.load_id == load_id
            )

        driver_bid_exists = exists(
            select(1)
            .select_from(DriverBid)
            .where(*driver_bid_conditions)
        )

        # --------------------------------------------------------------
        # Driver bid price
        # --------------------------------------------------------------

        driver_price_conditions = [
            DriverBid.vehicle_id == Vehicle.id,
            DriverBid.is_deleted.is_(False),
        ]

        if load_id is not None:
            driver_price_conditions.append(
                DriverBid.load_id == load_id
            )

        driver_bid_price = (
            select(
                DriverBid.driver_price
            )
            .where(*driver_price_conditions)
            .limit(1)
            .scalar_subquery()
        )

        # --------------------------------------------------------------
        # Owner bid
        # --------------------------------------------------------------

        owner_bid_exists = exists(
            select(1)
            .select_from(DriverBid)
            .where(
                DriverBid.vehicle_id == Vehicle.id,
                DriverBid.owner_bid.is_(True),
                DriverBid.is_deleted.is_(False),
                *(
                    [DriverBid.load_id == load_id]
                    if load_id is not None
                    else []
                ),
            )
        )

        # --------------------------------------------------------------
        # Is on load
        # --------------------------------------------------------------

        is_on_load_exists = exists(
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

        # --------------------------------------------------------------
        # Common columns
        # --------------------------------------------------------------

        common_columns = [
            driver_bid_exists.label(
                "is_driver_bid_vehicle"
            ),
            driver_bid_price.label(
                "driver_bid_price"
            ),
            owner_bid_exists.label(
                "owner_bid"
            ),
            is_on_load_exists.label(
                "is_on_load"
            ),
        ]

        if params.vehicle_ids and not (
            params.show_only_selected
        ):

            common_columns.append(
                Vehicle.id.in_(
                    params.vehicle_ids
                ).label(
                    "is_selected_vehicle"
                )
            )

        # ==============================================================
        # BID
        # ==============================================================

        if is_bid:

            return await self._distance_bid_list(
                target=target,
                radius_meters=radius_meters,
                vehicle_id=vehicle_id,
                driver_bid_exists=driver_bid_exists,
                common_columns=common_columns,
                params=params,
                matching_vehicle_type=matching_vehicle_type,
                matching_weight=matching_weight,
            )

        # ==============================================================
        # NORMAL DISTANCE LIST
        # ==============================================================

        return await self._distance_normal_list(
            target=target,
            radius_meters=radius_meters,
            driver_bid_exists=driver_bid_exists,
            common_columns=common_columns,
            params=params,
            matching_vehicle_type=matching_vehicle_type,
            matching_weight=matching_weight,
        )

    # ------------------------------------------------------------------
    # Bid distance list
    # ------------------------------------------------------------------

    async def _distance_bid_list(
        self,
        target,
        radius_meters: float | None,
        vehicle_id: int | None,
        driver_bid_exists,
        common_columns,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ) -> tuple[int, list[VehicleSchema]]:

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        ]

        # --------------------------------------------------------------
        # Selected
        # --------------------------------------------------------------

        if params.vehicle_ids and show_only_selected:

            conditions.append(
                or_(
                    Vehicle.id.in_(params.vehicle_ids),
                    driver_bid_exists,
                )
            )

        # --------------------------------------------------------------
        # Type
        # --------------------------------------------------------------

        if matching_vehicle_type:

            conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------------
        # Weight
        # --------------------------------------------------------------

        if matching_weight is not None:

            conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # --------------------------------------------------------------
        # Team
        # --------------------------------------------------------------

        team_condition = self._team_condition(
            is_bid=True,
            vehicle_id=vehicle_id,
        )

        if team_condition is not None:
            conditions.append(team_condition)

        # --------------------------------------------------------------
        # Distance
        #
        # ST_DWithin uses GIST.
        # --------------------------------------------------------------

        within_radius = func.ST_DWithin(
            Vehicle.location,
            target,
            radius_meters,
        )

        if radius_meters is not None:

            conditions.append(
                or_(
                    Vehicle.id == vehicle_id
                    if vehicle_id is not None
                    else literal(False),

                    driver_bid_exists,

                    within_radius,
                )
            )

        # --------------------------------------------------------------
        # Distance
        # --------------------------------------------------------------

        distance = (
            func.ST_Distance(
                Vehicle.location,
                target,
            )
            / MILES_TO_METERS
        )

        is_requested = (
            Vehicle.id == vehicle_id
            if vehicle_id is not None
            else literal(False)
        )

        stmt = (
            select(
                Vehicle.id.label("vid"),
                cast(
                    distance,
                    Float,
                ).label("sky_distance"),
                literal("current").label(
                    "location_type"
                ),
                is_requested.label(
                    "is_requested_vehicle"
                ),
                *common_columns,
            )
            .where(and_(*conditions))
        )

        # --------------------------------------------------------------
        # Ordering
        # --------------------------------------------------------------

        ordering = []

        if params.vehicle_ids and not show_only_selected:

            ordering.append(
                Vehicle.id.in_(
                    params.vehicle_ids
                ).desc()
            )

        ordering.extend(
            [
                is_requested.desc(),
                driver_bid_exists.desc(),
                distance.asc(),
            ]
        )

        stmt = stmt.order_by(*ordering)

        return await self._materialise(
            stmt,
            params,
        )

    # ------------------------------------------------------------------
    # Normal distance list
    # ------------------------------------------------------------------

    async def _distance_normal_list(
        self,
        target,
        radius_meters: float | None,
        driver_bid_exists,
        common_columns,
        params: VehicleListParams,
        matching_vehicle_type: str | None,
        matching_weight: int | None,
    ) -> tuple[int, list[VehicleSchema]]:

        show_only_selected = (
            params.show_only_selected
            and bool(params.vehicle_ids)
        )

        # ==============================================================
        # CURRENT
        # ==============================================================

        current_conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
            Vehicle.location.is_not(None),
        ]

        # --------------------------------------------------------------
        # Selected
        # --------------------------------------------------------------

        if params.vehicle_ids and show_only_selected:

            current_conditions.append(
                or_(
                    Vehicle.id.in_(params.vehicle_ids),
                    driver_bid_exists,
                )
            )

        # --------------------------------------------------------------
        # Type
        # --------------------------------------------------------------

        if matching_vehicle_type:

            current_conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------------
        # Weight
        # --------------------------------------------------------------

        if matching_weight is not None:

            current_conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # --------------------------------------------------------------
        # Team
        # --------------------------------------------------------------

        team_condition = self._team_condition(
            is_bid=False,
            vehicle_id=None,
        )

        if team_condition is not None:

            current_conditions.append(
                team_condition
            )

        # --------------------------------------------------------------
        # GIST spatial filter
        # --------------------------------------------------------------

        current_within = func.ST_DWithin(
            Vehicle.location,
            target,
            radius_meters,
        )

        if radius_meters is not None:

            current_conditions.append(
                or_(
                    driver_bid_exists,
                    current_within,
                )
            )

        # --------------------------------------------------------------
        # Distance
        # --------------------------------------------------------------

        current_distance = (
            func.ST_Distance(
                Vehicle.location,
                target,
            )
            / MILES_TO_METERS
        )

        current = select(
            Vehicle.id.label("vid"),
            cast(
                current_distance,
                Float,
            ).label(
                "sky_distance"
            ),
            literal("current").label(
                "location_type"
            ),
            literal(None).label(
                "is_requested_vehicle"
            ),
            *common_columns,
        ).where(
            and_(*current_conditions)
        )

        # ==============================================================
        # PLANNED
        # ==============================================================

        planned_conditions = [
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
            Vehicle.planned_location.is_not(None),
        ]

        # --------------------------------------------------------------
        # Selected
        # --------------------------------------------------------------

        if params.vehicle_ids and show_only_selected:

            planned_conditions.append(
                or_(
                    Vehicle.id.in_(params.vehicle_ids),
                    driver_bid_exists,
                )
            )

        # --------------------------------------------------------------
        # Type
        # --------------------------------------------------------------

        if matching_vehicle_type:

            planned_conditions.append(
                Vehicle.type.has(
                    func.upper(VehicleType.name)
                    == matching_vehicle_type.upper()
                )
            )

        # --------------------------------------------------------------
        # Weight
        # --------------------------------------------------------------

        if matching_weight is not None:

            planned_conditions.append(
                Vehicle.payload_lbs >= matching_weight
            )

        # --------------------------------------------------------------
        # Team
        # --------------------------------------------------------------

        if team_condition is not None:

            planned_conditions.append(
                team_condition
            )

        # --------------------------------------------------------------
        # GIST spatial filter
        # --------------------------------------------------------------

        planned_within = func.ST_DWithin(
            Vehicle.planned_location,
            target,
            radius_meters,
        )

        if radius_meters is not None:

            planned_conditions.append(
                or_(
                    driver_bid_exists,
                    planned_within,
                )
            )

        # --------------------------------------------------------------
        # Distance
        # --------------------------------------------------------------

        planned_distance = (
            func.ST_Distance(
                Vehicle.planned_location,
                target,
            )
            / MILES_TO_METERS
        )

        planned = select(
            Vehicle.id.label("vid"),
            cast(
                planned_distance,
                Float,
            ).label(
                "sky_distance"
            ),
            literal("planned").label(
                "location_type"
            ),
            literal(None).label(
                "is_requested_vehicle"
            ),
            *common_columns,
        ).where(
            and_(*planned_conditions)
        )

        # ==============================================================
        # UNION
        # ==============================================================

        unioned = current.union_all(
            planned
        ).subquery("veh")

        # ==============================================================
        # ORDER
        # ==============================================================

        ordering = []

        if params.vehicle_ids and not show_only_selected:

            ordering.append(
                unioned.c.is_selected_vehicle.desc()
            )

        ordering.extend(
            [
                unioned.c.is_driver_bid_vehicle.desc(),
                unioned.c.sky_distance.asc(),
            ]
        )

        stmt = (
            select(unioned)
            .order_by(*ordering)
        )

        return await self._materialise(
            stmt,
            params,
        )

    # ------------------------------------------------------------------
    # Materialise
    # ------------------------------------------------------------------

    async def _materialise(
        self,
        ordered_stmt,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # Don't use COUNT(*) OVER() here.
        #
        # First get the page.
        # --------------------------------------------------------------

        offset = (
            (params.page - 1)
            * params.page_size
        )

        page_stmt = (
            ordered_stmt
            .offset(offset)
            .limit(params.page_size)
        )

        rows = (
            await self.session.execute(
                page_stmt
            )
        ).mappings().all()

        if not rows:
            return 0, []

        # --------------------------------------------------------------
        # IDs
        # --------------------------------------------------------------

        vids = [
            row["vid"]
            for row in rows
        ]

        # --------------------------------------------------------------
        # Count separately
        # --------------------------------------------------------------

        count_stmt = select(
            func.count()
        ).select_from(
            ordered_stmt.subquery("counted")
        )

        count = await self.session.scalar(
            count_stmt
        )

        # --------------------------------------------------------------
        # Load actual Vehicle objects
        #
        # Only page IDs.
        # --------------------------------------------------------------

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

        # --------------------------------------------------------------
        # Preserve SQL ordering
        # --------------------------------------------------------------

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