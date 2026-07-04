"""OddsPapi v5 REST client - minimal wrapper."""
import os
import httpx
from typing import Optional

import mock_data

BASE_URL = "https://v5.oddspapi.io/en"
TENNIS_SPORT_ID = 12


class OddsPapiClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ["ODDSPAPI_KEY"]
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict | list:
        params = {**params, "apiKey": self.api_key}
        resp = await self._client.get(f"{BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_tennis_fixtures_upcoming(
        self,
        start_from_epoch: int,
        start_to_epoch: int,
        bookmakers: list[str],
    ) -> list[dict]:
        """Return pre-game tennis fixtures whose start is between the two epoch seconds.
        statusId=0 means Pre-Game (not live)."""
        if self.use_mock:
            fixtures = mock_data.build_mock_fixtures()
            return [f for f in fixtures if start_from_epoch <= f["startTime"] <= start_to_epoch]
        return await self._get(
            "/fixtures",
            {
                "sportId": TENNIS_SPORT_ID,
                "statusId": 0,
                "startTimeFrom": start_from_epoch,
                "startTimeTo": start_to_epoch,
                "bookmakers": ",".join(bookmakers),
            },
        )

    async def get_fixture_odds(self, fixture_id: str, bookmakers: list[str]) -> dict:
        if self.use_mock:
            return mock_data.build_mock_odds(fixture_id, bookmakers)
        return await self._get(
            "/fixtures/odds",
            {
                "fixtureId": fixture_id,
                "bookmakers": ",".join(bookmakers),
                "marketActive": "true",
            },
        )

    async def get_markets_for_sport(self, sport_id: int = TENNIS_SPORT_ID) -> list[dict]:
        if self.use_mock:
            return mock_data.build_mock_markets()
        return await self._get("/markets", {"sportId": sport_id})
