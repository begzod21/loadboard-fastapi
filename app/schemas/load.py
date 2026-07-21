from __future__ import annotations

import datetime
import decimal

from pydantic import BaseModel, ConfigDict, field_validator

from ..models.load import Load

from ..schemas.company import TenantCompanyOut


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


class LoadPointSchema(BaseModel):

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str | None = None
    order: int | None = None
    address: str | None = None
    latitude: decimal.Decimal | None = None
    longitude: decimal.Decimal | None = None
    state: str | None = None
    zip_code: str | None = None
    date: datetime.datetime | None = None


class BidInfoSchema(BaseModel):
    vehicle_id: int | None = None
    created_at: datetime.datetime | None = None
    dispatcher_name: str | None = None
    driver_name: str | None = None
    driver_price: float | None = 0
    broker_price: float | None = 0


class LoadDetailSchema(BaseModel):
    id: int
    default_message_on_bid: str | None = None
    pick_up_at: str | None = None
    pick_up_date_raw: str | None = None
    deliver_to: str | None = None
    delivery_date_raw: str | None = None
    miles: int | None = None
    pieces: int | None = None
    weight: int | None = None
    dims: object | None = None
    stackable: bool | None = None
    hazardous: bool | None = None
    fast_load: bool | None = None
    dock_level: bool | None = None
    suggested_truck: str | None = None
    notes: str | None = None
    contact_name: str | None = None
    expire_date: datetime.datetime | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_person: str | None = None
    expire_date_raw: str | None = None
    broker_company: int | None = None
    broker_rating: int | None = None
    posted_amount: float | None = None
    received_date: datetime.datetime | None = None
    bid_info: list[BidInfoSchema] | None = None
    order_number: str | None = None
    bid_link: str | None = None
    points: list[LoadPointSchema] = []
    broker_notes: str | None = None
    map_url: str | None = None

    @field_validator("dims", mode="before")
    @classmethod
    def validate_dims(cls, value: object) -> object | None:
        if value is None:
            return None
        if isinstance(value, (list, tuple, dict)):
            return value
        if isinstance(value, str):
            try:
                import json

                return json.loads(value)
            except (TypeError, ValueError):
                return value
        return value

    @classmethod
    def from_load(
        cls,
        load: Load,
        *,
        bid_info: list[BidInfoSchema] | None = None,
        company_data = None,
    ) -> "LoadDetailSchema":
        if company_data is not None:
            company = company_data
            bid_message = getattr(company, "bid_message", None)
            if bid_message:
                mc_number = getattr(company, "mc_number", None)
                if mc_number:
                    default_message_on_bid = bid_message.replace("[mc]", str(mc_number))
                else:
                    default_message_on_bid = bid_message

        return cls(
            id=load.id,
            default_message_on_bid=default_message_on_bid,
            pick_up_at=load.pick_up_at,
            pick_up_date_raw=load.pick_up_date_raw,
            deliver_to=load.deliver_to,
            delivery_date_raw=load.delivery_date_raw,
            miles=load.miles,
            pieces=load.pieces,
            weight=load.weight,
            dims=load.dims,
            stackable=load.stackable,
            hazardous=load.hazardous,
            fast_load=load.fast_load,
            dock_level=load.dock_level,
            suggested_truck=load.suggested_truck,
            notes=load.notes,
            contact_name=load.contact_name,
            expire_date=load.expire_date,
            contact_email=load.contact_email,
            contact_phone=load.contact_phone,
            contact_person=load.contact_person,
            expire_date_raw=load.expire_date_raw,
            broker_company=load.broker_company_id,
            broker_rating=load.broker_company.rating if load.broker_company else None,
            posted_amount=load.posted_amount,
            received_date=load.received_date,
            bid_info=bid_info,
            order_number=load.order_number,
            bid_link=load.bid_link,
            points=[LoadPointSchema.model_validate(p) for p in load.points],
            broker_notes=load.broker_company.notes if load.broker_company else None,
            map_url=(
                "https://www.google.com/maps/dir/"
                f"{load.pick_up_latitude},{load.pick_up_longitude}/"
                f"{load.deliver_to_latitude},{load.deliver_to_longitude}"
            ),
        )
