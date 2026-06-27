from __future__ import annotations

import datetime
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import ColumnElement, Float, cast, func

from ..models.load import Load


def _parse_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class LoadFilter:
    pick_up_at_address: str | None = None
    deliver_to_address: str | None = None
    pick_up_at_state: str | None = None
    deliver_to_state: str | None = None
    vehicle_type: str | None = None
    distance_type: str | None = None  # 'gte' | 'lte'
    distance_mile: float | None = None
    brokerage_type: str | None = None  # 'abs' | other
    brokerage: str | None = None
    pick_up_date: str | None = None
    deliver_date: str | None = None
    # address proximity (Haversine)
    address_radius: float | None = None
    lat: float | None = None
    lon: float | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []

        if self.pick_up_at_address:
            clauses.append(Load.pick_up_at.ilike(f"%{self.pick_up_at_address}%"))
        if self.deliver_to_address:
            clauses.append(Load.deliver_to.ilike(f"%{self.deliver_to_address}%"))
        if self.pick_up_at_state:
            states = [s for s in self.pick_up_at_state.split(",") if s]
            clauses.append(Load.pick_up_at_state.in_(states))
        if self.deliver_to_state:
            states = [s for s in self.deliver_to_state.split(",") if s]
            clauses.append(Load.deliver_to_state.in_(states))
        if self.vehicle_type:
            types = [v.upper() for v in self.vehicle_type.split(",")]
            clauses.append(Load.vehicle_type.in_(types))
        if self.distance_type and self.distance_mile is not None:
            if self.distance_type == "gte":
                clauses.append(Load.miles >= self.distance_mile)
            elif self.distance_type == "lte":
                clauses.append(Load.miles <= self.distance_mile)
        if self.brokerage_type and self.brokerage:
            if self.brokerage_type == "abs":
                clauses.append(Load.contact_name.ilike(f"%{self.brokerage}%"))
            else:
                clauses.append(~Load.contact_name.ilike(f"%{self.brokerage}%"))
        pick = _parse_date(self.pick_up_date)
        if pick:
            clauses.append(func.date(Load.pick_up_date) == pick)
        deliver = _parse_date(self.deliver_date)
        if deliver:
            clauses.append(func.date(Load.delivery_date) == deliver)

        if self.address_radius and self.lat is not None and self.lon is not None:
            clauses.append(self._haversine_clause())

        return clauses

    def _haversine_clause(self) -> ColumnElement[bool]:
        lat = float(self.lat)  # type: ignore[arg-type]
        lon = float(self.lon)  # type: ignore[arg-type]
        radius = float(self.address_radius)  # type: ignore[arg-type]
        lat_f = cast(Load.pick_up_latitude, Float)
        lon_f = cast(Load.pick_up_longitude, Float)
        sky = 3958.756 * func.acos(
            func.cos(func.radians(lat)) * func.cos(func.radians(lat_f))
            * func.cos(func.radians(lon_f) - func.radians(lon))
            + func.sin(func.radians(lat)) * func.sin(func.radians(lat_f))
        )
        return sky <= radius


def load_filter_params(
    pick_up_at_address: str | None = Query(default=None),
    deliver_to_address: str | None = Query(default=None),
    pick_up_at_state: str | None = Query(default=None),
    deliver_to_state: str | None = Query(default=None),
    vehicle_type: str | None = Query(default=None),
    distance_type: str | None = Query(default=None),
    distance_mile: float | None = Query(default=None),
    brokerage_type: str | None = Query(default=None),
    brokerage: str | None = Query(default=None),
    pick_up_date: str | None = Query(default=None),
    deliver_date: str | None = Query(default=None),
    address_radius: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
) -> LoadFilter:
    return LoadFilter(
        pick_up_at_address=pick_up_at_address,
        deliver_to_address=deliver_to_address,
        pick_up_at_state=pick_up_at_state,
        deliver_to_state=deliver_to_state,
        vehicle_type=vehicle_type,
        distance_type=distance_type,
        distance_mile=distance_mile,
        brokerage_type=brokerage_type,
        brokerage=brokerage,
        pick_up_date=pick_up_date,
        deliver_date=deliver_date,
        address_radius=address_radius,
        lat=lat,
        lon=lon,
    )
