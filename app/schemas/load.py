from __future__ import annotations

import datetime
import decimal

from pydantic import BaseModel, ConfigDict, field_validator

from ..models.load import Load

from ..schemas.company import TenantCompanyOut


def _build_default_message_on_bid(bid_message: object | None, mc_number: object | None) -> str | None:
    if not bid_message:
        return None

    text = str(bid_message)
    if not mc_number or "[mc]" not in text:
        return text

    return text.replace("[mc]", str(mc_number))


def _extract_company_message_data(company_data: object | None) -> tuple[object | None, object | None]:
    if company_data is None:
        return None, None

    if hasattr(company_data, "get"):
        return company_data.get("bid_message"), company_data.get("mc_number")

    return getattr(company_data, "bid_message", None), getattr(company_data, "mc_number", None)


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
        company_data: object | None = None,
    ) -> "LoadDetailSchema":
        default_message_on_bid = None
        if company_data is not None:
            bid_message, mc_number = _extract_company_message_data(company_data)
            default_message_on_bid = _build_default_message_on_bid(bid_message, mc_number)

        return cls(
            id=load.id,
            default_message_on_bid="<div style=\"font-family:Arial, sans-serif; width:100%; height:100%;\">\r\n    <table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" border=\"0\"\r\n           style=\"max-width:600px; width:100%; background-color:#ffffff; border-radius:10px; overflow:hidden; border: 1px solid #cfcfcf\">\r\n\r\n      <tr>\r\n          <td style=\"border-top: 20px solid #1e82ba; padding:20px 30px; text-align:left; color:#333333; font-size:14px; line-height:1.6; height: auto;\">\r\n          <p><b>RATE:</b> <span id=\"broker_price\">$[broker_price]</span></p>\r\n          <p><b>DIMENSIONS:</b> <span id=\"dimension\">[dimension]</span></p>\r\n          <p><b>MILES OUT:</b> <span id=\"miles\">[miles]</span></p>\r\n          <p><b>MC:</b> <span id=\"mc\">846834</span></p>\r\n          <p><b>VEHICLE:</b> <span id=\"vehicle_type\">[vehicle_type]</span></p>\r\n          <p><b>Truck equipment:</b> <span id=\"equipment\">[equipment]</span></p>\r\n          <span style=\"display: inline-block; background:#ffffff; color: #1e82ba; margin-top: 6px; font-weight: bold; padding: 10px 8px;border: 2px solid #1e82ba; border-radius: 0px;\">\r\n          ALL BIDS ARE VALID 15 MINUTES!\r\n        </span>\r\n        </td>\r\n      </tr>\r\n\r\n      <tr>\r\n        <td style=\"padding:0 30px; height: auto;\">\r\n          <hr style=\"border:0; height:1px; background-color:#e6e6e6;\">\r\n        </td>\r\n      </tr>\r\n\r\n      <tr>\r\n      <td style=\"padding:20px 30px; font-size:0; height: auto;\">\r\n        <div style=\"display:inline-block; vertical-align:middle; width:180px; margin-right:20px;\">\r\n          <img src=\"https://atrek-tms.s3.amazonaws.com/cargo_empire/media/logo/logo_color.jpg\"\r\n               alt=\"Logo\" width=\"180\"\r\n               style=\"border-radius:8px; border:1px solid #ddd; display:block;\">\r\n        </div>\r\n        <div style=\"display:inline-block; vertical-align:top; font-size:14px; color:#333333;\">\r\n          <p style=\"margin:4px 0; color:#000000; font-weight:bold;\">SHIPLUXE LLC</p>\r\n          <p style=\"margin:4px 0;\">MC <span class=\"wmi-callto\">846834</span></p>\r\n          <p style=\"margin:6px 0;\">Address: 10921 Reed Har tman Highway STE 323,</p>\r\n          <p style=\"margin:6px 0;\">Cincinnati, OH 45242</p>\r\n          <p style=\"margin:6px 0;\">Phone: 630-426-3362</p>\r\n          <p style=\"margin:6px 0;\">operation@shipluxellc.com</p>\r\n        </div>\r\n      </td>\r\n    </tr>\r\n\r\n\r\n      <tr>\r\n        <td style=\"padding:20px 30px; font-size:14px; color:#333333; height: auto;\">\r\n          <p style=\"margin:0; font-weight:bold; font-size:16px; color:#000000;\">[dispatcher_name]</p>\r\n          <p style=\"margin:6px 0;\"><b>&#9993;:</b> <a href=\"mailto:[dispatcher_email]\" style=\"color:#2b5876; text-decoration:none;\">[dispatcher_email]</a></p>\r\n          <p style=\"margin:6px 0;\"><b>&#9742;:</b> <span class=\"wmi-callto\">[dispatcher_phone]</span></p>\r\n        </td>\r\n      </tr>\r\n\r\n      <tr>\r\n        <td style=\"padding:20px; text-align:center; background-color:#f9fafc; font-size:12px; color:#999999; height: auto;\">\r\n          © SHIPLUXE LLC\r\n        </td>\r\n      </tr>\r\n    </table>\r\n  </div>",
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
