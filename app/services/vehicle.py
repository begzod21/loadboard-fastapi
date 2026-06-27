"""Port of ``VehicleListAPIView.get_queryset`` business logic.

Reproduces the Django behaviour:

* plain listing (status=1, registration_status=4) with ``VehicleFilter`` when no
  coordinates are supplied;
* proximity search using the Haversine formula (``with_distance`` /
  ``with_planned_distance``) when a coordinate / address / load_id / bid_id is
  supplied, including the ``driver_bid_price`` / ``owner_bid`` / ``is_on_load``
  annotations from ``with_common_annotations`` and team scoping.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Float,
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

from ..core.config import settings
from ..core.security import CurrentUser
from ..filters.vehicle import VehicleFilter
from ..models.load import Bid, ConfirmedLoad, DriverBid, Load
from ..models.vehicle import Vehicle
from ..schemas.vehicle import VehicleSchema
from .mapbox import MapService

EARTH_RADIUS_MILES = 3958.756


def _haversine(lat_col, lon_col, lat: float, lon: float):
    """SQLAlchemy expression equal to the RawSQL Haversine used by the model."""
    lat_f = cast(lat_col, Float)
    lon_f = cast(lon_col, Float)
    inner = (
        func.cos(func.radians(lat)) * func.cos(func.radians(lat_f))
        * func.cos(func.radians(lon_f) - func.radians(lon))
        + func.sin(func.radians(lat)) * func.sin(func.radians(lat_f))
    )
    clamped = func.least(1, func.greatest(-1, inner))
    return cast(EARTH_RADIUS_MILES * func.acos(clamped), Float)


@dataclass
class VehicleListParams:
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    radius: float | None = None
    load_id: int | None = None
    bid_id: int | None = None
    page: int = 1
    page_size: int = 20


class VehicleListService:
    def __init__(self, session: AsyncSession, user: CurrentUser) -> None:
        self.session = session
        self.user = user
        self.team_ids = user.team_ids
        self.map_service = MapService()

    async def list(
        self, params: VehicleListParams, filters: VehicleFilter
    ) -> tuple[int, list[VehicleSchema]]:
        latitude = params.latitude
        longitude = params.longitude

        if params.address:
            longitude, latitude = await self.map_service.get_coordinates(params.address)
            if longitude is None and latitude is None:
                return 0, []

        driver_bid_vehicle_ids: list[int] = []
        vehicle_id: int | None = None

        if params.load_id:
            load = await self.session.get(Load, params.load_id)
            if load is None:
                raise LookupError(f"Load not found! ID: {params.load_id}")
            if load.pick_up_longitude is not None and load.pick_up_latitude is not None:
                longitude = float(load.pick_up_longitude)
                latitude = float(load.pick_up_latitude)
            driver_bid_vehicle_ids = await self._driver_bid_vehicle_ids(params.load_id)

        if params.bid_id:
            bid = await self.session.get(Bid, params.bid_id)
            if bid is None:
                raise LookupError(f"Bid not found! ID: {params.bid_id}")
            if not bid.load_id:
                raise LookupError("This bid has no Load!")
            load = await self.session.get(Load, bid.load_id)
            if load and load.pick_up_longitude is not None and load.pick_up_latitude is not None:
                longitude = float(load.pick_up_longitude)
                latitude = float(load.pick_up_latitude)
            vehicle_id = bid.vehicle_id

        if latitude is not None and longitude is not None:
            radius = params.radius if params.radius is not None else settings.default_cargo_distance
            return await self._distance_list(
                float(latitude),
                float(longitude),
                radius,
                vehicle_id,
                driver_bid_vehicle_ids,
                bool(params.bid_id),
                params.load_id,
                params,
            )

        return await self._plain_list(filters, params)

    async def _plain_list(
        self, filters: VehicleFilter, params: VehicleListParams
    ) -> tuple[int, list[VehicleSchema]]:
        base = and_(
            Vehicle.status == 1,
            Vehicle.registration_status == 4,
            Vehicle.is_deleted.is_(False),
        )
        combined = filters.combined()
        where = and_(base, combined) if combined is not None else base

        count = await self.session.scalar(
            select(func.count()).select_from(Vehicle).where(where)
        )
        stmt = (
            select(Vehicle)
            .where(where)
            .order_by(Vehicle.id.desc())
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
            .options(selectinload(Vehicle.equipment))
        )
        vehicles = (await self.session.scalars(stmt)).unique().all()
        results = [VehicleSchema.from_vehicle(v) for v in vehicles]
        return int(count or 0), results

    async def _distance_list(
        self,
        lat: float,
        lon: float,
        radius: float | None,
        vehicle_id: int | None,
        driver_bid_vehicle_ids: list[int],
        is_bid: bool,
        load_id: int | None,
        params: VehicleListParams,
    ) -> tuple[int, list[VehicleSchema]]:
        effective_radius = 300 if radius == -1 else radius

        load_clause = [DriverBid.load_id == load_id] if load_id else []
        dbp = (
            select(DriverBid.driver_price)
            .where(DriverBid.vehicle_id == Vehicle.id, *load_clause)
            .limit(1)
            .scalar_subquery()
        )
        owner_bid_col = exists(
            select(DriverBid.id).where(
                DriverBid.vehicle_id == Vehicle.id,
                DriverBid.owner_bid.is_(True),
                *load_clause,
            )
        )
        is_on_load_col = exists(
            select(ConfirmedLoad.id).where(
                ConfirmedLoad.vehicle_id == Vehicle.id,
                ConfirmedLoad.status.in_([1, 2, 3, 4]),
            )
        )
        is_dbv = Vehicle.id.in_(driver_bid_vehicle_ids) if driver_bid_vehicle_ids else literal(False)

        def base_filter():
            cond = and_(
                Vehicle.status == 1,
                Vehicle.registration_status == 4,
                Vehicle.is_deleted.is_(False),
            )
            if vehicle_id:
                cond = or_(cond, Vehicle.id == vehicle_id)
            return cond

        def team_filter():
            if not self.team_ids:
                return None
            cond = or_(Vehicle.team_id.in_(self.team_ids), Vehicle.team_id.is_(None))
            if is_bid and vehicle_id:
                cond = or_(cond, Vehicle.id == vehicle_id)
            return cond

        common_cols = [
            is_dbv.label("is_driver_bid_vehicle"),
            dbp.label("driver_bid_price"),
            owner_bid_col.label("owner_bid"),
            is_on_load_col.label("is_on_load"),
        ]

        if is_bid:
            sky = _haversine(Vehicle.latitude, Vehicle.longitude, lat, lon)
            is_requested = (Vehicle.id == vehicle_id) if vehicle_id is not None else literal(False)
            sel = select(
                Vehicle.id.label("vid"),
                sky.label("sky_distance"),
                literal("current").label("location_type"),
                is_requested.label("is_requested_vehicle"),
                *common_cols,
            ).where(base_filter())
            tf = team_filter()
            if tf is not None:
                sel = sel.where(tf)
            if effective_radius is not None:
                sel = sel.where(
                    or_(
                        is_requested,
                        is_dbv,
                        sky <= effective_radius,
                    )
                )
            ordered = sel.order_by(
                literal_column_desc("is_requested_vehicle"),
                literal_column_desc("is_driver_bid_vehicle"),
                literal_column_asc("sky_distance"),
            )
            return await self._materialise(ordered, params)

        sky_cur = _haversine(Vehicle.latitude, Vehicle.longitude, lat, lon)
        sky_pln = _haversine(Vehicle.planned_latitude, Vehicle.planned_longitude, lat, lon)
        radius_filter_cur = (
            or_(is_dbv, sky_cur <= effective_radius) if effective_radius is not None else None
        )
        radius_filter_pln = (
            or_(is_dbv, sky_pln <= effective_radius) if effective_radius is not None else None
        )

        cur = select(
            Vehicle.id.label("vid"),
            sky_cur.label("sky_distance"),
            literal("current").label("location_type"),
            literal(None).label("is_requested_vehicle"),
            *common_cols,
        ).where(base_filter())

        pln = select(
            Vehicle.id.label("vid"),
            sky_pln.label("sky_distance"),
            literal("planned").label("location_type"),
            literal(None).label("is_requested_vehicle"),
            *common_cols,
        ).where(
            base_filter(),
            Vehicle.planned_latitude.is_not(None),
            Vehicle.planned_longitude.is_not(None),
            Vehicle.planned_address.is_not(None),
            Vehicle.planned_address != "",
        )

        tf = team_filter()
        if tf is not None:
            cur = cur.where(tf)
            pln = pln.where(tf)
        if radius_filter_cur is not None:
            cur = cur.where(radius_filter_cur)
            pln = pln.where(radius_filter_pln)

        unioned = union_all(cur, pln).subquery("veh")
        ordered = (
            select(unioned)
            .order_by(
                unioned.c.is_driver_bid_vehicle.desc(),
                unioned.c.sky_distance.asc(),
            )
        )
        return await self._materialise(ordered, params)

    async def _materialise(self, ordered_stmt, params):
        count = await self.session.scalar(
            select(func.count()).select_from(ordered_stmt.subquery())
        )
        page_stmt = ordered_stmt.offset(
            (params.page - 1) * params.page_size
        ).limit(params.page_size)

        rows = (await self.session.execute(page_stmt)).mappings().all()
        if not rows:
            return int(count or 0), []

        id_key = "vid" if "vid" in rows[0] else "id"
        vids = [row[id_key] for row in rows]

        vehicles = (
            await self.session.scalars(
                select(Vehicle)
                .where(Vehicle.id.in_(vids))
                .options(selectinload(Vehicle.equipment))
            )
        ).unique().all()
        by_id = {v.id: v for v in vehicles}

        results: list[VehicleSchema] = []
        for row in rows:
            vehicle = by_id.get(row[id_key])
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
        return int(count or 0), results

    async def _driver_bid_vehicle_ids(self, load_id: int) -> list[int]:
        stmt = select(DriverBid.vehicle_id).where(
            DriverBid.load_id == load_id,
            DriverBid.vehicle_id.is_not(None),
        )
        if self.team_ids:
            stmt = stmt.join(Vehicle, Vehicle.id == DriverBid.vehicle_id).where(
                or_(Vehicle.team_id.in_(self.team_ids), Vehicle.team_id.is_(None))
            )
        return [vid for vid in (await self.session.scalars(stmt)).all() if vid is not None]


def literal_column_desc(name: str):
    from sqlalchemy import literal_column

    return literal_column(name).desc()


def literal_column_asc(name: str):
    from sqlalchemy import literal_column

    return literal_column(name).asc()
