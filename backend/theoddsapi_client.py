"""The Odds API (v4) provider.

Exposes get_pinnacle_matches() returning the normalized shape the monitor uses:
    {match_id, tournament, player1, player2, start_epoch, selections:[
        {market_key, market_name, outcome, point, label, price}]}

Tennis is per-tournament sport keys (e.g. "tennis_atp_wimbledon"); a scan lists
the active tennis keys via the free /sports endpoint then fetches odds for each
(regions=eu, markets=h2h,totals -> 2 credits per key). Quota is read from the
x-requests-remaining header. Key: THE_ODDS_API_KEY, falling back to ODDSPAPI_KEY.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import httpx

import mock_data

BASE_URL = "https://api.the-odds-api.com/v4"
# Canonical sport key -> The Odds API sport-key prefix.
SPORT_PREFIXES = {"tennis": "tennis_", "basketball": "basketball_"}
DEFAULT_REGIONS = "eu"
DEFAULT_MARKETS = "h2h,totals"

H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total"


def _iso_epoch(value) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


class TheOddsApiClient:
    name = "theoddsapi"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (
            api_key
            or os.environ.get("THE_ODDS_API_KEY")
            or os.environ.get("ODDSPAPI_KEY")
        )
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)
        self.requests_remaining: int | None = None
        self.requests_used: int | None = None
        self.quota_exhausted = False

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict):
        if not self.api_key:
            raise RuntimeError("The Odds API key not configured (THE_ODDS_API_KEY)")
        params = {**params, "apiKey": self.api_key}
        resp = await self._client.get(f"{BASE_URL}{path}", params=params)
        for attr, header in (("requests_remaining", "x-requests-remaining"),
                             ("requests_used", "x-requests-used")):
            val = resp.headers.get(header)
            if val is not None:
                try:
                    setattr(self, attr, int(val))
                except ValueError:
                    pass
        # 401 (invalid/over-quota key) or 429 signal the key can't serve.
        if resp.status_code in (401, 429):
            self.quota_exhausted = True
        resp.raise_for_status()
        self.quota_exhausted = (
            self.requests_remaining is not None and self.requests_remaining <= 0
        )
        return resp.json()

    async def get_sports(self) -> list[dict]:
        data = await self._get("/sports", {})
        return data if isinstance(data, list) else []

    async def get_events(self, prefix: str, regions=DEFAULT_REGIONS, markets=DEFAULT_MARKETS) -> list[dict]:
        sports = await self.get_sports()
        keys = [s["key"] for s in sports
                if s.get("active") and str(s.get("key", "")).startswith(prefix)]
        events: list[dict] = []
        for key in keys:
            try:
                data = await self._get(
                    f"/sports/{key}/odds",
                    {"regions": regions, "markets": markets, "oddsFormat": "decimal"},
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (404, 422):
                    continue
                raise
            if isinstance(data, list):
                events.extend(data)
        return events

    async def get_pinnacle_matches(self, sport: str, start_epoch: int, end_epoch: int,
                                   tournament_filter=None) -> list[dict]:
        """Normalized Pinnacle matches (H2H + totals) for a sport in the window."""
        if self.use_mock:
            return mock_data.build_mock_pinnacle_matches(sport, start_epoch, end_epoch)
        prefix = SPORT_PREFIXES.get(sport, "tennis_")
        events = await self.get_events(prefix)
        out: list[dict] = []
        for ev in events:
            st = _iso_epoch(ev.get("commence_time"))
            if st is None or not (start_epoch < st <= end_epoch):
                continue
            if tournament_filter:
                name = (ev.get("sport_title") or "").lower()
                if not any(p in name for p in tournament_filter):
                    continue
            book = next((b for b in ev.get("bookmakers") or []
                         if b.get("key") == "pinnacle"), None)
            if not book:
                continue
            selections: list[dict] = []
            for market in book.get("markets") or []:
                mkey = market.get("key")
                if mkey == "h2h":
                    for o in market.get("outcomes") or []:
                        price, name = o.get("price"), o.get("name")
                        if price is None or not name:
                            continue
                        selections.append({
                            "market_key": "h2h", "market_name": H2H_MARKET_NAME,
                            "outcome": name, "point": None, "label": name,
                            "price": float(price),
                        })
                elif mkey == "totals":
                    for o in market.get("outcomes") or []:
                        price, name, point = o.get("price"), o.get("name"), o.get("point")
                        if price is None or not name:
                            continue
                        selections.append({
                            "market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                            "outcome": name, "point": point,
                            "label": f"{name} {point}" if point is not None else name,
                            "price": float(price),
                        })
            if selections:
                out.append({
                    "match_id": ev.get("id"),
                    "tournament": ev.get("sport_title"),
                    "player1": ev.get("home_team"),
                    "player2": ev.get("away_team"),
                    "start_epoch": st,
                    "selections": selections,
                })
        return out
