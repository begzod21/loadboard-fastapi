import re
from dataclasses import dataclass, field

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_tenant_db


def parse_team_filter(val: list[str] | str | None) -> tuple[list[int], bool]:
    """Parse company_teams or personal_teams query params.
    Returns (list_of_ids, has_all_flag).
    """
    if val is None:
        return [], False
    if isinstance(val, str):
        items = [val]
    elif isinstance(val, (list, tuple, set)):
        items = list(val)
    else:
        items = [str(val)]

    ids: list[int] = []
    has_all: bool = False

    for item in items:
        if isinstance(item, str):
            parts = [p.strip() for p in re.split(r"[,/|]", item) if p.strip()]
        else:
            parts = [str(item).strip()]

        for part in parts:
            if part.isdigit():
                ids.append(int(part))
            elif part.lower() in ("true", "t", "yes", "y", "1", "all"):
                has_all = True

    return ids, has_all


@dataclass
class CurrentUser:
    team_ids: list[int] = field(default_factory=list)
    company_team_ids: list[int] = field(default_factory=list)
    personal_team_id: int | None = None
    user_id: int | None = None
    user_uuid: str | None = None
    is_superuser: bool = False
    permissions: set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.team_ids and not self.company_team_ids and self.personal_team_id is None:
            self.company_team_ids = [t for t in self.team_ids if t is not None]

    @property
    def has_company_teams(self) -> bool:
        return bool(self.company_team_ids)

    def get_vehicle_team_condition(
        self,
        company_teams: list[str] | str | None = None,
        personal_teams: list[str] | str | None = None,
    ):
        from sqlalchemy import and_, or_, select, true
        from app.models.vehicle import Team, Vehicle

        personal_subquery = select(Team.id).where(
            or_(Team.is_personal.is_(True), Team.user_id.is_not(None))
        )

        if self.is_superuser:
            if self.has_company_teams:
                base_perm = or_(
                    Vehicle.team_id.is_(None),
                    Vehicle.team_id.in_(self.company_team_ids),
                    Vehicle.team_id.in_(personal_subquery),
                )
            else:
                base_perm = true()
        elif self.has_company_teams:
            allowed_ids = list(self.company_team_ids)
            if self.personal_team_id:
                allowed_ids.append(self.personal_team_id)
            base_perm = or_(
                Vehicle.team_id.in_(allowed_ids),
                Vehicle.team_id.is_(None),
            )
        else:
            company_subquery = select(Team.id).where(
                or_(Team.is_personal.is_(False), Team.is_personal.is_(None)),
                Team.user_id.is_(None),
            )
            conds = [
                Vehicle.team_id.is_(None),
                Vehicle.team_id.in_(company_subquery),
            ]
            if self.personal_team_id:
                conds.append(Vehicle.team_id == self.personal_team_id)
            base_perm = or_(*conds)

        c_ids, c_all = parse_team_filter(company_teams)
        p_ids, p_all = parse_team_filter(personal_teams)

        if not (c_ids or c_all or p_ids or p_all):
            return base_perm

        filter_conds = []
        if c_ids:
            filter_conds.append(Vehicle.team_id.in_(c_ids))
        elif c_all:
            comp_sub = select(Team.id).where(
                or_(Team.is_personal.is_(False), Team.is_personal.is_(None)),
                Team.user_id.is_(None),
            )
            filter_conds.append(Vehicle.team_id.in_(comp_sub))
        elif p_ids or p_all:
            if self.company_team_ids:
                filter_conds.append(Vehicle.team_id.in_(self.company_team_ids))
            else:
                comp_sub = select(Team.id).where(
                    or_(Team.is_personal.is_(False), Team.is_personal.is_(None)),
                    Team.user_id.is_(None),
                )
                filter_conds.append(Vehicle.team_id.in_(comp_sub))

        if p_ids:
            filter_conds.append(Vehicle.team_id.in_(p_ids))
        elif p_all:
            filter_conds.append(Vehicle.team_id.in_(personal_subquery))
        if self.personal_team_id is not None:
            filter_conds.append(Vehicle.team_id == self.personal_team_id)

        if not filter_conds:
            return base_perm

        return and_(base_perm, or_(*filter_conds))

    def get_load_team_condition(self):
        from sqlalchemy import and_, exists, or_, select
        from app.models.load import Load, load_vehicle_teams
        from app.models.vehicle import Team

        personal_subquery = select(Team.id).where(
            or_(Team.is_personal.is_(True), Team.user_id.is_not(None))
        )

        if self.is_superuser:
            if not self.has_company_teams:
                return None
            has_team = exists(
                select(load_vehicle_teams.c.id).where(
                    load_vehicle_teams.c.load_id == Load.id,
                    or_(
                        load_vehicle_teams.c.team_id.in_(self.company_team_ids),
                        load_vehicle_teams.c.team_id.in_(personal_subquery),
                    ),
                )
            )
            return or_(
                Load.has_driver_in_all_teams.is_(True),
                and_(
                    or_(
                        Load.has_driver_in_all_teams.is_(False),
                        Load.has_driver_in_all_teams.is_(None),
                    ),
                    has_team,
                ),
            )

        allowed_ids = list(self.company_team_ids)
        if self.personal_team_id:
            allowed_ids.append(self.personal_team_id)

        if not allowed_ids:
            return None

        has_team = exists(
            select(load_vehicle_teams.c.id).where(
                load_vehicle_teams.c.load_id == Load.id,
                load_vehicle_teams.c.team_id.in_(allowed_ids),
            )
        )
        return or_(
            Load.has_driver_in_all_teams.is_(True),
            and_(
                or_(
                    Load.has_driver_in_all_teams.is_(False),
                    Load.has_driver_in_all_teams.is_(None),
                ),
                has_team,
            ),
        )


def _credentials_exception(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_token(authorization: str | None) -> dict:
    if not authorization:
        raise _credentials_exception("Not authenticated")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _credentials_exception("Invalid authentication credentials")

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise _credentials_exception("Invalid or expired token") from exc
    return payload


async def get_current_user(
    session: AsyncSession = Depends(get_tenant_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    payload = _decode_token(authorization)
    try:
        user_id = int(payload["user_id"]) if payload.get("user_id") is not None else None
    except (TypeError, ValueError) as exc:
        raise _credentials_exception("Invalid or expired token") from exc
    if user_id is None:
        raise _credentials_exception("Invalid or expired token")

    result = await session.execute(
        text("""
            SELECT
                u.is_superuser,
                u.uuid AS user_uuid,
                (
                    SELECT array_agg(DISTINCT ut.team_id)
                    FROM user_user_teams ut
                    JOIN user_team t ON t.id = ut.team_id
                    WHERE ut.user_id = u.id
                      AND (t.is_personal IS FALSE OR t.is_personal IS NULL)
                      AND t.user_id IS NULL
                ) AS company_team_ids,
                (
                    SELECT t.id
                    FROM user_team t
                    WHERE t.is_personal IS TRUE
                      AND (
                          t.user_id = u.id
                          OR t.id IN (SELECT ut.team_id FROM user_user_teams ut WHERE ut.user_id = u.id)
                      )
                    LIMIT 1
                ) AS personal_team_id,
                (
                    SELECT array_agg(DISTINCT ut.team_id)
                    FROM user_user_teams ut
                    WHERE ut.user_id = u.id
                ) AS team_ids,
                (
                    SELECT array_agg(DISTINCT p.codename)
                    FROM user_user_user_permissions up
                    JOIN auth_permission p ON p.id = up.permission_id
                    WHERE up.user_id = u.id
                ) AS permissions
            FROM user_user u
            WHERE u.id = :user_id
        """),
        {"user_id": user_id},
    )

    row = result.first()

    if row is None:
        raise _credentials_exception("User not found")

    user_uuid = row.user_uuid

    user = CurrentUser(
        user_id=user_id,
        user_uuid=str(user_uuid) if user_uuid is not None else None,
        is_superuser=row.is_superuser,
        team_ids=[t for t in (row.team_ids or []) if t is not None],
        company_team_ids=[t for t in (row.company_team_ids or []) if t is not None],
        personal_team_id=row.personal_team_id,
        permissions=set(row.permissions or []),
    )
    return user