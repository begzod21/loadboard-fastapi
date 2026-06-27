from __future__ import annotations

import httpx

_GEOCODE_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"


class MapService:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    async def get_coordinates(self, address: str) -> tuple[float | None, float | None]:
        """Return ``(longitude, latitude)`` for an address, or ``(None, None)``."""
        if not self.token or not address:
            return None, None

        params = {"access_token": self.token, "limit": 1}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _GEOCODE_URL.format(query=address),
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError:
            return None, None

        features = data.get("features") or []
        if not features:
            return None, None
        lon, lat = features[0]["center"]
        return lon, lat

    async def get_address_by_zip(self, zip_code: str) -> str | None:
        """Resolve a ZIP/postal code to a formatted place name."""
        if not self.token or not zip_code:
            return None
        params = {"access_token": self.token, "limit": 1, "types": "postcode"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _GEOCODE_URL.format(query=zip_code), params=params
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError:
            return None
        features = data.get("features") or []
        return features[0]["place_name"] if features else None
