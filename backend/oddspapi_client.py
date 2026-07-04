"""OddsPapi v4 public REST client.

Host: https://api.oddspapi.io/v4 (the public v4 API the account key is valid for).

Only three endpoints are used by the monitor:

* GET /tournaments?sportId=12
      -> list of tournaments with upcoming/future/live fixture counts.
* GET /odds-by-tournaments?bookmaker=<one>&tournamentIds=a,b,c
      -> list of fixtures, each with `bookmakerOdds -> {book} -> markets ->
         {marketId} -> outcomes -> {outcomeId} -> players.0.price`.
      Exactly ONE bookmaker is allowed per request, so soft books are fetched
      with one call each.
* GET /participants?sportId=12
      -> a flat {participantId: name} map for the whole sport (the
         participantIds filter is ignored server-side), cached in-process.

The endpoints are rate limited to roughly one request per second each and reply
with HTTP 429 + a JSON body carrying `error.retryMs`; `_get` honours that with a
short sequential back-off. Callers must NOT fan these out concurrently.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import mock_data

BASE_URL = "https://api.oddspapi.io/v4"
TENNIS_SPORT_ID = 12

# The v4 API rejects more than 5 tournamentIds per /odds-by-tournaments call.
# Some bookmakers cap it lower (Betfair Exchange allows only 3).
TOURNAMENT_CHUNK_SIZE = 5
_BOOKMAKER_CHUNK_LIMITS = {"betfair-ex": 3}


def _to_iso(epoch: int) -> str:
    """Epoch seconds -> ISO-8601 UTC timestamp (what /fixtures expects)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# Each endpoint is rate limited to ~1 request/second; pace requests to stay just
# under that and avoid wasting a round-trip on a 429 every time.
_MIN_REQUEST_INTERVAL = 1.05
# Max 429 retries before giving up on a single request.
_MAX_RETRIES = 5


class OddsPapiClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ["ODDSPAPI_KEY"]
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)
        self._participants_cache: dict[str, str] | None = None
        self._next_request_at = 0.0

    async def close(self):
        await self._client.aclose()

    async def _pace(self):
        """Serialise requests to stay under the ~1 req/s rate limit."""
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_request_at = time.monotonic() + _MIN_REQUEST_INTERVAL

    async def _get(self, path: str, params: dict) -> Any:
        """GET with the api key attached, retrying on 429 rate-limit replies."""
        params = {**params, "apiKey": self.api_key}
        url = f"{BASE_URL}{path}"
        for attempt in range(_MAX_RETRIES + 1):
            await self._pace()
            resp = await self._client.get(url, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                await asyncio.sleep(self._retry_delay(resp))
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                err = data["error"] or {}
                raise RuntimeError(
                    f"OddsPapi v4 error on {path}: "
                    f"{err.get('message')} ({err.get('code')})"
                )
            return data
        # Exhausted retries on 429.
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _retry_delay(resp: httpx.Response) -> float:
        """Seconds to wait before retrying a 429, from the JSON body if present."""
        try:
            body = resp.json()
            retry_ms = (body.get("error") or {}).get("retryMs")
            if retry_ms:
                return float(retry_ms) / 1000.0 + 0.1
        except Exception:
            pass
        return 1.1

    # -- Fixtures ------------------------------------------------------------

    async def get_fixtures(
        self,
        start_from_epoch: int,
        start_to_epoch: int,
        sport_id: int = TENNIS_SPORT_ID,
    ) -> list[dict]:
        """Scheduled fixtures whose start falls in [from, to] (epoch seconds).

        This is the authoritative, fresh schedule: it already carries player and
        tournament names, and it excludes the stale phantom fixtures that
        /odds-by-tournaments can still list. One call replaces the previous
        tournament sweep + participants lookup.
        """
        if self.use_mock:
            return mock_data.build_mock_fixtures(start_from_epoch, start_to_epoch)
        data = await self._get(
            "/fixtures",
            {
                "sportId": sport_id,
                "from": _to_iso(start_from_epoch),
                "to": _to_iso(start_to_epoch),
            },
        )
        return data if isinstance(data, list) else []

    # -- Tournaments ---------------------------------------------------------

    async def get_tournaments(self, sport_id: int = TENNIS_SPORT_ID) -> list[dict]:
        """All tennis tournaments with fixture counts."""
        if self.use_mock:
            return mock_data.build_mock_tournaments()
        data = await self._get("/tournaments", {"sportId": sport_id})
        return data if isinstance(data, list) else []

    # -- Odds ----------------------------------------------------------------

    async def get_odds_by_tournaments(
        self,
        bookmaker: str,
        tournament_ids: list[int],
        chunk_size: int = TOURNAMENT_CHUNK_SIZE,
    ) -> list[dict]:
        """Fixtures + odds for one bookmaker across the given tournaments.

        The endpoint accepts a single bookmaker and a comma-separated list of
        tournamentIds. Long lists are split into chunks fetched sequentially
        (the endpoint rate-limits concurrent calls hard).
        """
        if self.use_mock:
            return mock_data.build_mock_odds(bookmaker, tournament_ids)
        if not tournament_ids:
            return []
        chunk_size = min(chunk_size, _BOOKMAKER_CHUNK_LIMITS.get(bookmaker, chunk_size))
        fixtures: list[dict] = []
        for i in range(0, len(tournament_ids), chunk_size):
            chunk = tournament_ids[i : i + chunk_size]
            try:
                data = await self._get(
                    "/odds-by-tournaments",
                    {
                        "bookmaker": bookmaker,
                        "tournamentIds": ",".join(str(t) for t in chunk),
                    },
                )
            except httpx.HTTPStatusError as e:
                # 404 means none of the tournaments in this chunk currently have
                # odds for this bookmaker (a chunk with any covered tournament
                # returns 200) - simply skip it.
                if e.response.status_code == 404:
                    continue
                raise
            if isinstance(data, list):
                fixtures.extend(data)
        return fixtures

    # -- Participants --------------------------------------------------------

    async def get_participants(
        self, sport_id: int = TENNIS_SPORT_ID, force: bool = False
    ) -> dict[str, str]:
        """{participantId: name} for the whole sport, cached in-process.

        The v4 endpoint ignores any participantIds filter and returns the full
        catalogue (~700 KB for tennis), so it is fetched once and reused.
        """
        if self.use_mock:
            return mock_data.build_mock_participants()
        if self._participants_cache is not None and not force:
            return self._participants_cache
        data = await self._get("/participants", {"sportId": sport_id})
        self._participants_cache = data if isinstance(data, dict) else {}
        return self._participants_cache
