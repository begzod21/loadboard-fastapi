"""Pydantic schema mirroring the DRF ``LoadListSerializer`` output."""
from __future__ import annotations

import datetime
import decimal

from pydantic import BaseModel, ConfigDict

from ..models.load import Load


class LoadListSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    received_date: datetime.datetime | None = None
    pick_up_at: str | None = None
    deliver_to: str | None = None
    suggested_truck: str | None = None
    miles: int | None = None
    contact_name: str | None = None
    source_name: str | None = None
    vehicle_type: str | None = None

    is_read: bool | None = None
    is_pinned: bool | None = None
    is_bid: bool | None = None
    is_driver_bid: bool | None = None

    pick_up_at_state: str | None = None
    pick_up_date: datetime.datetime | None = None
    pick_up_latitude: decimal.Decimal | None = None
    pick_up_longitude: decimal.Decimal | None = None
    deliver_to_state: str | None = None
    delivery_date: datetime.datetime | None = None

    miles_out: int | None = None
    nearest_vehicles_count: int | None = None
    radius: float | None = None
    broker_rating: int | None = None
    count_day: int | None = None
    vehicle_teams: list[int] = []
    broker_company: int | None = None
    has_driver_in_all_teams: bool | None = None

    @classmethod
    def from_load(
        cls,
        load: Load,
        *,
        is_bid: bool | None = None,
        is_driver_bid: bool | None = None,
        is_read: bool | None = None,
        is_pinned: bool | None = None,
        radius: float | None = None,
    ) -> "LoadListSchema":
        return cls(
            id=load.id,
            received_date=load.received_date,
            pick_up_at=load.pick_up_at,
            deliver_to=load.deliver_to,
            suggested_truck=load.suggested_truck,
            miles=load.miles,
            contact_name=load.contact_name,
            source_name=load.source_name,
            vehicle_type=load.vehicle_type,
            is_read=is_read,
            is_pinned=is_pinned,
            is_bid=is_bid,
            is_driver_bid=is_driver_bid,
            pick_up_at_state=load.pick_up_at_state,
            pick_up_date=load.pick_up_date,
            pick_up_latitude=load.pick_up_latitude,
            pick_up_longitude=load.pick_up_longitude,
            deliver_to_state=load.deliver_to_state,
            delivery_date=load.delivery_date,
            miles_out=load.miles_out,
            nearest_vehicles_count=load.nearest_vehicles_count,
            radius=radius,
            broker_rating=load.broker_company.rating if load.broker_company else None,
            count_day=load.count_day,
            vehicle_teams=[t.id for t in load.vehicle_teams],
            broker_company=load.broker_company_id,
            has_driver_in_all_teams=load.has_driver_in_all_teams,
        )


class PaginatedLoads(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list[LoadListSchema]
