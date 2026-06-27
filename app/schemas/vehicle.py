from __future__ import annotations

import datetime
import decimal

from pydantic import BaseModel, ConfigDict

from ..models.vehicle import Vehicle


class DriverForVehicleListSchema(BaseModel):
    """Equivalent of ``DriverForVehicleListSerializer``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str | None = None
    citizenship: str | None = None
    phone: str | None = None
    address: str | None = None
    birth: datetime.date | None = None
    email: str | None = None


class VehicleSchema(BaseModel):
    """Equivalent of ``VehicleSerializer`` (read representation)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    object_id: str | None = None
    driver: DriverForVehicleListSchema | None = None
    second_driver: DriverForVehicleListSchema | None = None

    owner_phone: str | None = None
    owner_name: str | None = None
    owner_company_name: str | None = None

    sky_distance: float | None = None
    road_distance: float | None = None

    type_name: str | None = None
    team: int | None = None
    team_name: str | None = None

    driver_bid_price: float | None = None
    owner_bid: bool | None = None
    is_on_load: bool | None = None
    is_requested_vehicle: bool | None = None

    road_duration: str | None = None

    equipment_names: str | None = None
    equipment_short_names: str | None = None

    status: int | None = None
    last_address: str | None = None
    last_geo_date_time: datetime.datetime | None = None
    notes: str | None = None

    type: int | None = None
    owner_company: int | None = None
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    useful_cargo_length: float | None = None
    useful_cargo_width: float | None = None
    useful_cargo_height: float | None = None
    payload_lbs: float | None = None
    door_width: float | None = None
    door_height: float | None = None

    location_type: str | None = None
    planned_address: str | None = None
    planned_date_time: datetime.datetime | None = None
    latitude: decimal.Decimal | None = None
    longitude: decimal.Decimal | None = None
    planned_latitude: decimal.Decimal | None = None
    planned_longitude: decimal.Decimal | None = None

    @classmethod
    def from_vehicle(
        cls,
        vehicle: Vehicle,
        *,
        sky_distance: float | None = None,
        location_type: str | None = None,
        driver_bid_price: float | None = None,
        owner_bid: bool | None = None,
        is_on_load: bool | None = None,
        is_requested_vehicle: bool | None = None,
        road_distance: float | None = None,
        road_duration: str | None = None,
    ) -> "VehicleSchema":
        """Build the schema from an ORM ``Vehicle`` plus query-time annotations.

        This mirrors how the DRF serializer combines model fields with the
        annotations injected by ``VehicleListAPIView.get_queryset``.
        """
        owner = vehicle.owner_company
        owner_name = None
        if owner is not None:
            first = owner.company_applicant_first_name or ""
            last = owner.company_applicant_last_name or ""
            full = f"{first} {last}".strip()
            owner_name = full or None

        return cls(
            id=vehicle.id,
            object_id=vehicle.object_id,
            driver=(
                DriverForVehicleListSchema.model_validate(vehicle.driver)
                if vehicle.driver
                else None
            ),
            second_driver=(
                DriverForVehicleListSchema.model_validate(vehicle.second_driver)
                if vehicle.second_driver
                else None
            ),
            owner_phone=owner.company_phone if owner else None,
            owner_name=owner_name,
            owner_company_name=owner.company_name if owner else None,
            sky_distance=sky_distance,
            road_distance=road_distance,
            type_name=vehicle.type.name if vehicle.type else None,
            team=vehicle.team_id,
            team_name=vehicle.team.name if vehicle.team else None,
            driver_bid_price=driver_bid_price,
            owner_bid=owner_bid,
            is_on_load=is_on_load,
            is_requested_vehicle=is_requested_vehicle,
            road_duration=road_duration,
            equipment_names=vehicle.equipment_names,
            equipment_short_names=vehicle.equipment_short_names,
            status=vehicle.status,
            last_address=vehicle.last_address,
            last_geo_date_time=vehicle.last_geo_date_time,
            notes=vehicle.notes,
            type=vehicle.type_id,
            owner_company=vehicle.owner_company_id,
            created_at=vehicle.created_at,
            updated_at=vehicle.updated_at,
            useful_cargo_length=vehicle.useful_cargo_length,
            useful_cargo_width=vehicle.useful_cargo_width,
            useful_cargo_height=vehicle.useful_cargo_height,
            payload_lbs=vehicle.payload_lbs,
            door_width=vehicle.door_width,
            door_height=vehicle.door_height,
            location_type=location_type,
            planned_address=vehicle.planned_address,
            planned_date_time=vehicle.planned_date_time,
            latitude=vehicle.latitude,
            longitude=vehicle.longitude,
            planned_latitude=vehicle.planned_latitude,
            planned_longitude=vehicle.planned_longitude,
        )


class PaginatedVehicles(BaseModel):
    """DRF-style paginated envelope (``CustomPagination``)."""

    count: int
    next: str | None = None
    previous: str | None = None
    results: list[VehicleSchema]
