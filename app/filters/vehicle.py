from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import Query
from sqlalchemy import ColumnElement, String, and_, cast, func, or_

from ..models.vehicle import Vehicle, VehicleType


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
    type: str | None = None
    types: str | None = None
    vehicle_type: str | None = None
    vehicle_types: str | None = None

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

        type_inputs = [
            t
            for t in [self.type, self.types, self.vehicle_type, self.vehicle_types]
            if t is not None
        ]
        if type_inputs:
            raw_vals: list[str] = []
            for ti in type_inputs:
                raw_vals.extend([v.strip() for v in re.split(r"[,/|]", str(ti)) if v.strip()])

            type_clauses: list[ColumnElement[bool]] = []
            ids = [int(v) for v in raw_vals if v.isdigit()]
            if ids:
                type_clauses.append(Vehicle.type_id.in_(ids))
                type_clauses.append(Vehicle.types.any(VehicleType.id.in_(ids)))

            names = [v.upper() for v in raw_vals if not v.isdigit()]
            if names:
                from ..services.load import TYPE_SYNONYMS

                expanded_names: set[str] = set(names)
                for n in names:
                    if n in TYPE_SYNONYMS:
                        expanded_names.update(TYPE_SYNONYMS[n])
                names_list = list(expanded_names)
                type_clauses.append(
                    Vehicle.type.has(func.upper(VehicleType.name).in_(names_list))
                )
                type_clauses.append(
                    Vehicle.types.any(func.upper(VehicleType.name).in_(names_list))
                )

            if type_clauses:
                clauses.append(or_(*type_clauses))

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
    type: str | None = Query(default=None),
    types: str | None = Query(default=None),
    vehicle_type: str | None = Query(default=None),
    vehicle_types: str | None = Query(default=None),
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
        type=type,
        types=types,
        vehicle_type=vehicle_type,
        vehicle_types=vehicle_types,
    )
