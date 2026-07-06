"""The Odds API (v4) REST client.

The Pinnacle drop-monitor only needs Pinnacle's h2h + totals for tennis, all of
which The Odds API provides. Tennis is exposed as per-tournament sport keys
(e.g. "tennis_atp_wimbledon"), so a scan lists the active tennis keys via the
free /sports endpoint and then fetches odds for each.

Quota: the /odds endpoint costs [markets] x [regions] credits per request
(h2h,totals on region eu = 2 credits). /sports is free. The remaining/used quota
is reported in the x-requests-remaining / x-requests-used response headers and
cached on the client.

The API key is read from THE_ODDS_API_KEY, falling back to ODDSPAPI_KEY so an
existing deployment keeps working after the provider switch.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

import mock_data

BASE_URL = "https://api.the-odds-api.com/v4"
TENNIS_PREFIX = "tennis_"
DEFAULT_REGIONS = "eu"          # Pinnacle is available in the EU region
DEFAULT_MARKETS = "h2h,totals"  # 2 credits per request


class TheOddsApiClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (
            api_key
            or os.environ.get("THE_ODDS_API_KEY")
            or os.environ["ODDSPAPI_KEY"]
        )
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)
        self.requests_remaining: int | None = None
        self.requests_used: int | None = None

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict):
        params = {**params, "apiKey": self.api_key}
        resp = await self._client.get(f"{BASE_URL}{path}", params=params)
        # Quota headers are present on both /sports and /odds responses.
        for attr, header in (("requests_remaining", "x-requests-remaining"),
                             ("requests_used", "x-requests-used")):
            val = resp.headers.get(header)
            if val is not None:
                try:
                    setattr(self, attr, int(val))
                except ValueError:
                    pass
        resp.raise_for_status()
        return resp.json()

    async def get_sports(self) -> list[dict]:
        """All sports/leagues (free, doesn't cost credits)."""
        if self.use_mock:
            return mock_data.build_mock_sports()
        data = await self._get("/sports", {})
        return data if isinstance(data, list) else []

    async def get_tennis_events(
        self,
        regions: str = DEFAULT_REGIONS,
        markets: str = DEFAULT_MARKETS,
    ) -> list[dict]:
        """Odds for every active tennis tournament (one /odds call per sport key)."""
        if self.use_mock:
            return mock_data.build_mock_events()
        sports = await self.get_sports()
        keys = [
            s["key"] for s in sports
            if s.get("active") and str(s.get("key", "")).startswith(TENNIS_PREFIX)
        ]
        events: list[dict] = []
        for key in keys:
            try:
                data = await self._get(
                    f"/sports/{key}/odds",
                    {"regions": regions, "markets": markets, "oddsFormat": "decimal"},
                )
            except httpx.HTTPStatusError as e:
                # 404/422 = no odds currently offered for this sport key; skip it.
                if e.response.status_code in (404, 422):
                    continue
                raise
            if isinstance(data, list):
                events.extend(data)
        return events
