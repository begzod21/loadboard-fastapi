from __future__ import annotations

import datetime
import decimal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base

load_vehicle_teams = Table(
    "load_load_vehicle_teams",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("load_id", ForeignKey("load_load.id")),
    Column("team_id", ForeignKey("user_team.id")),
)

load_is_read_users = Table(
    "load_load_is_read_users",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("load_id", ForeignKey("load_load.id")),
    Column("user_id", Integer),
)


class BrokerCompany(Base):
    __tablename__ = "broker_brokercompany"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rating: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String)


class Load(Base):
    __tablename__ = "load_load"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    received_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    pick_up_at: Mapped[str | None] = mapped_column(String(255))
    deliver_to: Mapped[str | None] = mapped_column(String(255))
    suggested_truck: Mapped[str | None] = mapped_column(String(255))
    miles: Mapped[int | None] = mapped_column(Integer)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    source_name: Mapped[str | None] = mapped_column(String(255))
    vehicle_type: Mapped[str | None] = mapped_column(String(255))

    pick_up_at_state: Mapped[str | None] = mapped_column(String(255))
    pick_up_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    pick_up_latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    pick_up_longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))

    deliver_to_state: Mapped[str | None] = mapped_column(String(255))
    delivery_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    deliver_to_latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    deliver_to_longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))

    pick_up_date_raw: Mapped[str | None] = mapped_column(String(255))
    delivery_date_raw: Mapped[str | None] = mapped_column(String(255))
    expire_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    expire_date_raw: Mapped[str | None] = mapped_column(String(255))
    pieces: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[int | None] = mapped_column(Integer)
    dims: Mapped[str | None] = mapped_column(String(255))
    stackable: Mapped[bool | None] = mapped_column(Boolean)
    hazardous: Mapped[bool | None] = mapped_column(Boolean)
    fast_load: Mapped[bool | None] = mapped_column(Boolean)
    dock_level: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(String)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(255))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    posted_amount: Mapped[float | None] = mapped_column(Float)
    order_number: Mapped[str | None] = mapped_column(String(255))
    bid_link: Mapped[str | None] = mapped_column(String)

    miles_out: Mapped[int | None] = mapped_column(Integer, default=0)
    nearest_vehicles_count: Mapped[int | None] = mapped_column(Integer, default=0)
    count_day: Mapped[int | None] = mapped_column(Integer, default=1)

    has_driver_in_all_teams: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    broker_company_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_brokercompany.id")
    )

    broker_company: Mapped[BrokerCompany | None] = relationship(lazy="joined")
    vehicle_teams: Mapped[list["Team"]] = relationship(  # noqa: F821
        "Team", secondary=load_vehicle_teams, lazy="selectin"
    )
    points: Mapped[list["LoadPoint"]] = relationship(
        "LoadPoint", order_by="LoadPoint.order", lazy="selectin"
    )


class LoadPoint(Base):
    __tablename__ = "load_loadpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    load_id: Mapped[int | None] = mapped_column(ForeignKey("load_load.id"))
    type: Mapped[str | None] = mapped_column(String(10))
    order: Mapped[int | None] = mapped_column(Integer)
    address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    state: Mapped[str | None] = mapped_column(String(255))
    zip_code: Mapped[str | None] = mapped_column(String(20))
    date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class Bid(Base):
    __tablename__ = "load_bid"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    load_id: Mapped[int | None] = mapped_column(ForeignKey("load_load.id"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("owner_vehicle.id"))
    dispatcher_id: Mapped[int | None] = mapped_column(Integer)
    driver_price: Mapped[float | None] = mapped_column(Float)
    broker_price: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class DriverBid(Base):
    __tablename__ = "load_driverbid"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    load_id: Mapped[int | None] = mapped_column(ForeignKey("load_load.id"))
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("owner_vehicle.id"))
    driver_price: Mapped[float | None] = mapped_column(Float)
    owner_bid: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_bid_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class ConfirmedLoad(Base):
    __tablename__ = "load_confirmedload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("owner_vehicle.id"))
    load_id: Mapped[int | None] = mapped_column(ForeignKey("load_load.id"))
    driver_price: Mapped[float | None] = mapped_column(Float)
