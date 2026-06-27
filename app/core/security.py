from dataclasses import dataclass, field

from fastapi import Depends, Header
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_tenant_db


@dataclass
class CurrentUser:
    team_ids: list[int] = field(default_factory=list)
    user_id: int | None = None


async def get_current_user(
    session: AsyncSession = Depends(get_tenant_db),
    x_team_ids: str | None = Header(default=None),
    x_user_id: int | None = Header(default=None),
    x_user_uuid: str | None = Header(default=None),
) -> CurrentUser:
    team_ids: list[int] = []

    if x_team_ids:
        for part in x_team_ids.split(","):
            part = part.strip()
            if part.isdigit():
                team_ids.append(int(part))

    user_id = x_user_id
    if user_id is None and x_user_uuid:
        user_row = await session.execute(
            text(
                """
                SELECT id
                FROM user_user
                WHERE uuid = :uuid
                LIMIT 1
                """
            ),
            {"uuid": x_user_uuid},
        )
        found = user_row.first()
        if found:
            user_id = int(found.id)

    if user_id is not None and not team_ids:
        rows = await session.execute(
            text(
                """
                SELECT team_id
                FROM user_user_teams
                WHERE user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        team_ids = [int(row.team_id) for row in rows if row.team_id is not None]

    return CurrentUser(team_ids=team_ids, user_id=user_id)
