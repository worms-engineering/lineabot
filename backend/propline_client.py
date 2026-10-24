"""PropLine provider - NBA player props from the sportsbooks.

Pinnacle prices no NBA player props on OddsPapi (verified), so props come from
PropLine (https://prop-line.com/docs), an odds API aggregating the US books.
The tracked price is one SOURCE book (FanDuel by default: the book that leads
price discovery on US props); the other books' prices for the same player /
stat / line / side ride along on each selection (`others`) so the alert can show
them without an extra request.

Quota: the free tier is 1,000 requests/day. The bulk endpoint
`/v1/sports/basketball_nba/odds?markets=...` returns every game of the slate in
ONE request, and it is only called while a game starts inside the scan window
(the schedule comes from `/events`, cached for SCHEDULE_TTL). A night of NBA
polled every minute is a few hundred requests. Prices are American odds and are
converted to decimal. Key: PROPLINE_API_KEY, or set from the dashboard.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import mock_data

logger = logging.getLogger(__name__)

BASE_URL = "https://api.prop-line.com/v1"
SPORT_KEYS = {"props_nba": "basketball_nba"}
# stat market key -> Italian label for the alert.
PROP_MARKETS = {
    "player_points": "Punti",
    "player_rebounds": "Rimbalzi",
    "player_assists": "Assist",
    "player_threes": "Triple",
    "player_points_rebounds_assists": "Punti+Rimbalzi+Assist",
}
SOURCE_BOOK = os.environ.get("PROPLINE_SOURCE_BOOK", "fanduel").strip().lower()
# Books whose price for the same prop is shown next to the source book's.
CONTEXT_BOOKS = ["draftkings", "betmgm", "bovada", "fanatics", "betrivers", "pinnacle"]
BOOK_LABELS = {
    "fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
    "bovada": "Bovada", "fanatics": "Fanatics", "betrivers": "BetRivers",
    "pinnacle": "Pinnacle", "unibet": "Unibet", "hardrock": "Hard Rock",
}
# Only track sides priced inside this decimal band: main prop lines sit around
# 1.70-2.30, and far-off prices are thin ladders where one tick is a big "move".
PRICE_BAND = (1.25, 5.0)
SCHEDULE_TTL = 30 * 60


def _book_label(key: str) -> str:
    return BOOK_LABELS.get(key, key)


def american_to_decimal(price: Any) -> float | None:
    try:
        a = float(price)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return round(1 + a / 100, 3)
    if a <= -100:
        return round(1 + 100 / abs(a), 3)
    return None  # American odds never sit inside (-100, 100)


def _outcome_decimal(o: dict) -> float | None:
    dec = o.get("price_decimal")
    if dec is not None:
        try:
            return round(float(dec), 3)
        except (TypeError, ValueError):
            pass
    return american_to_decimal(o.get("price"))


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


def _two_way_props(bookmaker: dict) -> dict[tuple, float]:
    """{(market, player, point, side): decimal price} for a book's two-way
    Over/Under player props. Milestone rungs ("25+ Points", point null) and
    YES-only lists have no opposite leg and are skipped."""
    out: dict[tuple, float] = {}
    for mk in bookmaker.get("markets") or []:
        key = mk.get("key")
        if key not in PROP_MARKETS or mk.get("period"):
            continue
        for o in mk.get("outcomes") or []:
            side = str(o.get("name") or "").lower()
            player = str(o.get("description") or "").strip()
            point = o.get("point")
            if side not in ("over", "under") or not player or point is None:
                continue
            dec = _outcome_decimal(o)
            if dec:
                out[(key, player, float(point), side)] = dec
    return out


def parse_props_events(events: list, start_epoch: int, end_epoch: int,
                       source_book: str = SOURCE_BOOK) -> list[dict]:
    """Normalize a bulk /odds payload into monitor matches: one match per game
    starting in (start_epoch, end_epoch], one selection per source-book prop
    side, each carrying the other books' prices for the same prop."""
    out: list[dict] = []
    for ev in events or []:
        if ev.get("live"):
            continue
        st = _iso_epoch(ev.get("commence_time"))
        if st is None or not (start_epoch < st <= end_epoch):
            continue
        books = {b.get("key"): _two_way_props(b) for b in ev.get("bookmakers") or []}
        src = books.get(source_book) or {}
        selections: list[dict] = []
        for (mkey, player, point, side), price in src.items():
            # Both sides must be on the board: a one-sided prop is a line the
            # book is pulling or hasn't fully posted.
            other_side = "under" if side == "over" else "over"
            if (mkey, player, point, other_side) not in src:
                continue
            if not (PRICE_BAND[0] <= price <= PRICE_BAND[1]):
                continue
            others = [{"book": _book_label(bk), "price": props[(mkey, player, point, side)]}
                      for bk, props in books.items()
                      if bk != source_book and (mkey, player, point, side) in props]
            label_side = "Over" if side == "over" else "Under"
            selections.append({
                "market_key": f"prop:{mkey}:{player}",
                "market_name": f"{PROP_MARKETS[mkey]} {player}",
                "outcome": side,
                "point": point,
                "label": f"{label_side} {point:g}",
                "price": price,
                "others": others,
            })
        if selections:
            out.append({
                "match_id": f"pl-{ev.get('id')}",
                "tournament": f"NBA · Player props ({_book_label(source_book)})",
                "player1": ev.get("home_team"),
                "player2": ev.get("away_team"),
                "start_epoch": st,
                "source_book": _book_label(source_book),
                "selections": selections,
            })
    return out


class PropLineClient:
    name = "propline"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("PROPLINE_API_KEY")
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)
        self.requests_remaining: int | None = None
        self.quota_exhausted = False
        self.ip_blocked = False
        # sport_key -> (monotonic fetch time, [commence epochs])
        self._schedule: dict[str, tuple[float, list[int]]] = {}
        self._warned_no_key = False

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("PropLine key not configured (PROPLINE_API_KEY)")
        resp = await self._client.get(f"{BASE_URL}{path}", params=params or {},
                                      headers={"X-API-Key": self.api_key})
        rem = resp.headers.get("X-Daily-Remaining")
        if rem is not None:
            try:
                self.requests_remaining = int(rem)
            except ValueError:
                pass
        if resp.status_code == 429:
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            if "burst" not in str(body.get("error") or ""):
                self.quota_exhausted = True  # daily cap: resets 00:00 UTC
            raise RuntimeError(f"PropLine 429 on {path}: {body.get('error') or resp.text[:120]}")
        if resp.status_code == 401:
            raise RuntimeError("PropLine rejected the API key (401)")
        resp.raise_for_status()
        self.quota_exhausted = (self.requests_remaining is not None
                                and self.requests_remaining <= 0)
        return resp.json()

    async def _game_times(self, sport_key: str) -> list[int]:
        """Commence times of the sport's upcoming games, cached SCHEDULE_TTL so
        the odds call can be skipped (zero quota) while nothing is in window."""
        now = time.monotonic()
        hit = self._schedule.get(sport_key)
        if hit is not None and now - hit[0] < SCHEDULE_TTL:
            return hit[1]
        data = await self._get(f"/sports/{sport_key}/events")
        events = data if isinstance(data, list) else (data or {}).get("data") or []
        times = [t for t in (_iso_epoch(e.get("commence_time")) for e in events) if t]
        self._schedule[sport_key] = (now, times)
        return times

    async def get_pinnacle_matches(self, sport: str, start_epoch: int, end_epoch: int,
                                   tournament_filter=None) -> list[dict]:
        sport_key = SPORT_KEYS.get(sport)
        if sport_key is None:
            return []
        if self.use_mock:
            return mock_data.build_mock_pinnacle_matches("basketball", start_epoch, end_epoch)
        if not self.api_key:
            if not self._warned_no_key:
                logger.info("PropLine key not set: NBA book props are idle")
                self._warned_no_key = True
            return []
        if not any(start_epoch < t <= end_epoch for t in await self._game_times(sport_key)):
            return []  # no game in the window: don't spend a request
        books = ",".join([SOURCE_BOOK] + [b for b in CONTEXT_BOOKS if b != SOURCE_BOOK])
        data = await self._get(f"/sports/{sport_key}/odds", {
            "markets": ",".join(PROP_MARKETS), "bookmakers": books})
        events = data if isinstance(data, list) else (data or {}).get("data") or []
        return parse_props_events(events, start_epoch, end_epoch)
