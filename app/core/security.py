from dataclasses import dataclass, field

from fastapi import Header


@dataclass
class CurrentUser:
    team_ids: list[int] = field(default_factory=list)
    user_id: int | None = None


def get_current_user(
    x_team_ids: str | None = Header(default=None),
    x_user_id: int | None = Header(default=None),
) -> CurrentUser:
    team_ids: list[int] = []
    if x_team_ids:
        for part in x_team_ids.split(","):
            part = part.strip()
            if part.isdigit():
                team_ids.append(int(part))
    return CurrentUser(team_ids=team_ids, user_id=x_user_id)
