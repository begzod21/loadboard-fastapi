from pydantic import BaseModel
from typing import Optional

class Company(BaseModel):
    id: int
    schema_name: str
    domain_url: str
    cargo_distance: Optional[float] = None
    mapbox_token: Optional[str] = None
    bid_message: Optional[str] = None
    mc_number: Optional[str] = None
