from dataclasses import dataclass, field

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_tenant_db


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

    def get_vehicle_team_condition(self):
        from sqlalchemy import and_, or_, select
        from app.models.vehicle import Team, Vehicle

        if self.has_company_teams:
            allowed_ids = list(self.company_team_ids)
            if self.personal_team_id:
                allowed_ids.append(self.personal_team_id)
            return or_(
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
            return or_(*conds)

    def get_load_team_condition(self):
        from sqlalchemy import and_, exists, or_, select
        from app.models.load import Load, load_vehicle_teams

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