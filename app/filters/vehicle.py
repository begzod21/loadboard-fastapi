from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import ColumnElement, String, and_, cast

from ..models.vehicle import Vehicle


@dataclass
class VehicleFilter:
    """Equivalent of ``app/owner/views/filters.py::VehicleFilter``."""

    id: int | None = None
    object_id: str | None = None
    owner_company_id: int | None = None
    driver_id: int | None = None
    status: int | None = None
    model: str | None = None
    make: str | None = None
    year: str | None = None
    is_deleted: bool | None = None

    def conditions(self) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        if self.id is not None:
            # django-filter uses icontains on id here (cast to text).
            clauses.append(cast(Vehicle.id, String).ilike(f"%{self.id}%"))
        if self.object_id:
            clauses.append(Vehicle.object_id.ilike(f"%{self.object_id}%"))
        if self.owner_company_id is not None:
            clauses.append(Vehicle.owner_company_id == self.owner_company_id)
        if self.driver_id is not None:
            clauses.append(Vehicle.driver_id == self.driver_id)
        if self.status is not None:
            clauses.append(Vehicle.status == self.status)
        if self.model:
            clauses.append(Vehicle.model.ilike(f"%{self.model}%"))
        if self.make:
            clauses.append(Vehicle.make.ilike(f"%{self.make}%"))
        if self.year:
            clauses.append(Vehicle.year == self.year)
        if self.is_deleted is not None:
            clauses.append(Vehicle.is_deleted.is_(self.is_deleted))
        return clauses

    def combined(self) -> ColumnElement[bool] | None:
        clauses = self.conditions()
        return and_(*clauses) if clauses else None


def vehicle_filter_params(
    id: int | None = Query(default=None),
    object_id: str | None = Query(default=None),
    owner_company_id: int | None = Query(default=None),
    driver_id: int | None = Query(default=None),
    status: int | None = Query(default=None),
    model: str | None = Query(default=None),
    make: str | None = Query(default=None),
    year: str | None = Query(default=None),
    is_deleted: bool | None = Query(default=None),
) -> VehicleFilter:
    return VehicleFilter(
        id=id,
        object_id=object_id,
        owner_company_id=owner_company_id,
        driver_id=driver_id,
        status=status,
        model=model,
        make=make,
        year=year,
        is_deleted=is_deleted,
    )
