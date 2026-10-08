from __future__ import annotations

import datetime
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import ColumnElement, Float, cast, exists, func, or_, select

from ..models.load import DriverBid, Load, load_vehicle_teams


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
    vehicle_types: str | None = None
    distance_type: str | None = None  # 'gte' | 'lte'
    distance_mile: float | None = None
    brokerage_type: str | None = None  # 'abs' | other
    brokerage: str | None = None
    broker_ids: str | None = None
    pick_up_date: str | None = None
    deliver_date: str | None = None
    # address proximity (Haversine)
    address_radius: float | None = None
    lat: float | None = None
    lon: float | None = None
    # vehicle & proximity filters
    radius: float | None = None
    vehicle_radius: float | None = None
    vehicle_ids: str | None = None
    has_matching_vehicles: bool | str = False
    show_only_selected: bool | str = False
    vehicle_team: str | None = None
    company_teams: list[str] | str | None = None
    personal_teams: list[str] | str | None = None
    is_driver_bid: str | None = None
    page: int | None = None
    timezone: int | None = None

    def conditions(self, include_teams: bool = True) -> list[ColumnElement[bool]]:
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
        if self.broker_ids:
            b_ids = [int(b.strip()) for b in self.broker_ids.split(",") if b.strip().isdigit()]
            if b_ids:
                if self.brokerage_type == "inc":
                    clauses.append(Load.broker_company_id.in_(b_ids))
                elif self.brokerage_type == "exc":
                    clauses.append(
                        or_(Load.broker_company_id.not_in(b_ids), Load.broker_company_id.is_(None))
                    )
        pick = _parse_date(self.pick_up_date)
        if pick:
            clauses.append(func.date(Load.pick_up_date) == pick)
        deliver = _parse_date(self.deliver_date)
        if deliver:
            clauses.append(func.date(Load.delivery_date) == deliver)

        from ..core.security import parse_team_filter
        from ..models.vehicle import Team

        c_ids, c_all = parse_team_filter(self.company_teams)
        p_ids, p_all = parse_team_filter(self.personal_teams)
        if self.vehicle_team:
            legacy_ids, _ = parse_team_filter(self.vehicle_team)
            c_ids.extend(legacy_ids)

        if include_teams and (c_ids or c_all or p_ids or p_all):
            team_clauses = []
            if c_ids:
                team_clauses.append(load_vehicle_teams.c.team_id.in_(c_ids))
            elif c_all:
                comp_sub = select(Team.id).where(
                    or_(Team.is_personal.is_(False), Team.is_personal.is_(None)),
                    Team.user_id.is_(None),
                )
                team_clauses.append(load_vehicle_teams.c.team_id.in_(comp_sub))

            if p_ids:
                team_clauses.append(load_vehicle_teams.c.team_id.in_(p_ids))
            elif p_all:
                pers_sub = select(Team.id).where(
                    or_(Team.is_personal.is_(True), Team.user_id.is_not(None))
                )
                team_clauses.append(load_vehicle_teams.c.team_id.in_(pers_sub))

            if team_clauses:
                clauses.append(
                    exists(
                        select(load_vehicle_teams.c.id).where(
                            load_vehicle_teams.c.load_id == Load.id,
                            or_(*team_clauses),
                        )
                    )
                )


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
    vehicle_types: str | None = Query(default=None, description="Vehicle pool by type"),
    distance_type: str | None = Query(default=None),
    distance_mile: float | None = Query(default=None),
    brokerage_type: str | None = Query(default=None),
    brokerage: str | None = Query(default=None),
    broker_ids: str | None = Query(default=None),
    pick_up_date: str | None = Query(default=None),
    deliver_date: str | None = Query(default=None),
    address_radius: float | None = Query(default=None),
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    radius: float | None = Query(default=None),
    vehicle_radius: float | None = Query(default=None),
    vehicle_ids: str | None = Query(default=None),
    has_matching_vehicles: bool | str = Query(default=False),
    show_only_selected: bool | str = Query(default=False),
    vehicle_team: str | None = Query(default=None),
    company_teams: list[str] | None = Query(default=None),
    personal_teams: list[str] | None = Query(default=None),
    is_driver_bid: str | None = Query(default=None),
    page: int | None = Query(default=None),
    timezone: int | None = Query(default=None),
) -> LoadFilter:
    return LoadFilter(
        pick_up_at_address=pick_up_at_address,
        deliver_to_address=deliver_to_address,
        pick_up_at_state=pick_up_at_state,
        deliver_to_state=deliver_to_state,
        vehicle_type=vehicle_type,
        vehicle_types=vehicle_types,
        distance_type=distance_type,
        distance_mile=distance_mile,
        brokerage_type=brokerage_type,
        brokerage=brokerage,
        broker_ids=broker_ids,
        pick_up_date=pick_up_date,
        deliver_date=deliver_date,
        address_radius=address_radius,
        lat=lat,
        lon=lon,
        radius=radius,
        vehicle_radius=vehicle_radius,
        vehicle_ids=vehicle_ids,
        has_matching_vehicles=has_matching_vehicles,
        show_only_selected=show_only_selected,
        vehicle_team=vehicle_team,
        company_teams=company_teams,
        personal_teams=personal_teams,
        is_driver_bid=is_driver_bid,
        page=page,
        timezone=timezone,
    )

