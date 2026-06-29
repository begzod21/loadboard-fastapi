from __future__ import annotations

import datetime
import decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base

vehicle_equipment = Table(
    "owner_vehicle_equipment",
    Base.metadata,
    Column("vehicle_id", ForeignKey("owner_vehicle.id"), primary_key=True),
    Column("equipment_id", ForeignKey("handbk_equipment.id"), primary_key=True),
)


class Equipment(Base):
    __tablename__ = "handbk_equipment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(255))


class VehicleType(Base):
    __tablename__ = "handbk_vehicletype"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))


class Team(Base):
    __tablename__ = "user_team"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))


class OwnerCompany(Base):
    __tablename__ = "owner_ownercompany"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str | None] = mapped_column(String(255))
    company_phone: Mapped[str | None] = mapped_column(String(255))
    company_applicant_first_name: Mapped[str | None] = mapped_column(String(255))
    company_applicant_last_name: Mapped[str | None] = mapped_column(String(255))


class Driver(Base):
    __tablename__ = "owner_driver"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    citizenship: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    birth: Mapped[datetime.date | None] = mapped_column(Date)
    email: Mapped[str | None] = mapped_column(String(255))


class Vehicle(Base):
    __tablename__ = "owner_vehicle"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_id: Mapped[str | None] = mapped_column(String(255))

    registration_status: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[int] = mapped_column(Integer, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    owner_company_id: Mapped[int | None] = mapped_column(
        ForeignKey("owner_ownercompany.id")
    )
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("owner_driver.id"))
    second_driver_id: Mapped[int | None] = mapped_column(ForeignKey("owner_driver.id"))
    type_id: Mapped[int | None] = mapped_column(ForeignKey("handbk_vehicletype.id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("user_team.id"))

    model: Mapped[str | None] = mapped_column(String(255))
    make: Mapped[str | None] = mapped_column(String(255))
    year: Mapped[str | None] = mapped_column(String(255))

    payload_lbs: Mapped[float | None] = mapped_column(Float)
    useful_cargo_length: Mapped[float | None] = mapped_column(Float)
    useful_cargo_width: Mapped[float | None] = mapped_column(Float)
    useful_cargo_height: Mapped[float | None] = mapped_column(Float)
    door_width: Mapped[float | None] = mapped_column(Float)
    door_height: Mapped[float | None] = mapped_column(Float)

    notes: Mapped[str | None] = mapped_column(Text)

    last_address: Mapped[str | None] = mapped_column(String(255))
    latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    last_geo_date_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    planned_address: Mapped[str | None] = mapped_column(String(255))
    planned_latitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    planned_longitude: Mapped[decimal.Decimal | None] = mapped_column(Numeric(9, 6))
    planned_date_time: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    owner_company: Mapped[OwnerCompany | None] = relationship(lazy="joined")
    driver: Mapped[Driver | None] = relationship(
        foreign_keys=[driver_id], lazy="joined"
    )
    second_driver: Mapped[Driver | None] = relationship(
        foreign_keys=[second_driver_id], lazy="joined"
    )
    type: Mapped[VehicleType | None] = relationship(lazy="joined")
    team: Mapped[Team | None] = relationship(lazy="joined")
    equipment: Mapped[list[Equipment]] = relationship(
        secondary=vehicle_equipment, lazy="selectin"
    )

    @property
    def equipment_names(self) -> str:
        try:
            return ", ".join(e.name for e in self.equipment if e.name)
        except Exception:
            return ""

    @property
    def equipment_short_names(self) -> str:
        try:
            return ", ".join(e.short_name for e in self.equipment if e.short_name)
        except Exception:
            return ""
