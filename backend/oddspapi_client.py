"""OddsPapi v4 provider.

Exposes get_pinnacle_matches() returning the normalized shape the monitor uses:
    {match_id, tournament, player1, player2, start_epoch, selections:[
        {market_key, market_name, outcome, point, label, price}]}

Covers the full tennis calendar (incl. Challenger/ITF). A scan uses one
/fixtures call (fresh schedule with names) then fetches Pinnacle odds only for
the tournaments with an in-window match. Endpoints are rate limited to ~1 req/s
and reply 429 + error.retryMs; _get paces + retries. Key: ODDSPAPI_KEY.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import mock_data

BASE_URL = "https://api.oddspapi.io/v4"
TENNIS_SPORT_ID = 12
# Canonical sport key -> OddsPapi sportId.
SPORT_IDS = {"tennis": 12, "basketball": 11, "football": 10}
TOURNAMENT_CHUNK_SIZE = 5
_BOOKMAKER_CHUNK_LIMITS = {"betfair-ex": 3}
_MIN_REQUEST_INTERVAL = 1.05
_MAX_RETRIES = 5

SHARP_BOOK = "pinnacle"
# Full-match/full-game totals sit above per-set (tennis) lines; combined with the
# "/0/totals" period check this isolates the whole-event total from quarters/sets.
MIN_TOTAL_LINE = 15.0
_TOTALS_OUTCOME_RE = re.compile(r"^(\d+(?:\.\d+)?)/(over|under)$")

H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total"


def _to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _single_book_odds(fixture: dict) -> dict | None:
    return next(iter((fixture.get("bookmakerOdds") or {}).values()), None)


def _is_real_event(fx: dict) -> bool:
    """Exclude simulated-reality / virtual events: Pinnacle never prices them."""
    text = f"{fx.get('categoryName') or ''} {fx.get('tournamentName') or ''}".lower()
    return not ("simulated" in text or "virtual" in text or "srl " in text)


def _matches_whitelist(fx: dict, patterns) -> bool:
    """True if the fixture matches the whitelist. Patterns can be:
    - a plain lowercase substring, matched against tournamentName only
      (e.g. basketball's "nba");
    - a (category_or_None, name) 2-tuple: exact (trimmed, case-insensitive)
      tournamentName match, optionally scoped to a category substring — used
      for domestic top flights, so "Bundesliga" doesn't also match
      "2. Bundesliga" the way a substring would;
    - a (category_or_None, name_substring, "contains") 3-tuple: substring
      match instead of exact — used for UEFA cups, so it still catches
      qualifying-round variants like "UEFA Europa League Qualification".
    A None category means "any" (international/UEFA competitions).
    """
    if not patterns:
        return True
    name = (fx.get("tournamentName") or "").strip().lower()
    category = (fx.get("categoryName") or "").lower()
    for p in patterns:
        if isinstance(p, tuple):
            if len(p) == 3:
                cat_pat, name_pat, _mode = p
                name_ok = name_pat in name
            else:
                cat_pat, name_pat = p
                name_ok = name == name_pat
            if name_ok and (cat_pat is None or cat_pat in category):
                return True
        elif p in name:
            return True
    return False


def _pinnacle_h2h(book_odds: dict) -> dict | None:
    """Sharp moneyline prices. Returns {"home": price, "away": price} and,
    for sports with a draw (football's 1X2), also {"draw": price}. Tennis and
    basketball fixtures simply never carry a "draw" outcome, so this is a
    no-op for them."""
    for market in (book_odds.get("markets") or {}).values():
        if market.get("marketActive") is False:
            continue
        if not (market.get("bookmakerMarketId") or "").endswith("/0/moneyline"):
            continue
        sides: dict[str, float] = {}
        for outcome in (market.get("outcomes") or {}).values():
            player = (outcome.get("players") or {}).get("0") or {}
            price = player.get("price")
            if price is None or player.get("active") is False:
                continue
            boid = player.get("bookmakerOutcomeId")
            if boid in ("home", "away", "draw"):
                sides[boid] = float(price)
        if "home" in sides and "away" in sides:
            return sides
    return None


def _pinnacle_main_total(book_odds: dict) -> dict | None:
    for market in (book_odds.get("markets") or {}).values():
        if market.get("marketActive") is False:
            continue
        # "/0/totals" = whole-event period: excludes tennis set totals and
        # basketball quarter/half totals (which use /1, /2, ...).
        if not (market.get("bookmakerMarketId") or "").endswith("/0/totals"):
            continue
        sides: dict[str, dict] = {}
        is_main = False
        for outcome in (market.get("outcomes") or {}).values():
            player = (outcome.get("players") or {}).get("0") or {}
            price = player.get("price")
            if price is None or player.get("active") is False:
                continue
            m = _TOTALS_OUTCOME_RE.match(player.get("bookmakerOutcomeId") or "")
            if not m:
                continue
            line = float(m.group(1))
            if line < MIN_TOTAL_LINE:
                continue
            if player.get("mainLine"):
                is_main = True
            sides[m.group(2)] = {"price": float(price), "line": line}
        if is_main and "over" in sides and "under" in sides:
            return {"line": sides["over"]["line"],
                    "over_price": sides["over"]["price"],
                    "under_price": sides["under"]["price"]}
    return None


class OddsPapiClient:
    name = "oddspapi"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ODDSPAPI_KEY")
        self.use_mock = os.environ.get("USE_MOCK_DATA", "false").lower() == "true"
        self._client = httpx.AsyncClient(timeout=30.0)
        self._next_request_at = 0.0
        self.requests_remaining: int | None = None  # OddsPapi has no quota header
        self.quota_exhausted = False

    async def close(self):
        await self._client.aclose()

    async def _pace(self):
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_request_at = time.monotonic() + _MIN_REQUEST_INTERVAL

    async def _get(self, path: str, params: dict) -> Any:
        if not self.api_key:
            raise RuntimeError("OddsPapi key not configured (ODDSPAPI_KEY)")
        params = {**params, "apiKey": self.api_key}
        url = f"{BASE_URL}{path}"
        for attempt in range(_MAX_RETRIES + 1):
            await self._pace()
            resp = await self._client.get(url, params=params)
            if resp.status_code == 429:
                # 429 is used both for transient rate limiting (RATE_LIMITED,
                # retry) and for the permanent monthly quota (REQUEST_LIMIT_
                # EXCEEDED, do not retry).
                try:
                    code = (resp.json().get("error") or {}).get("code")
                except Exception:
                    code = None
                if code == "REQUEST_LIMIT_EXCEEDED":
                    self.quota_exhausted = True
                    raise RuntimeError(f"OddsPapi quota exhausted on {path}: request limit reached")
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(self._retry_delay(resp))
                    continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                err = data["error"] or {}
                if err.get("code") == "REQUEST_LIMIT_EXCEEDED":
                    self.quota_exhausted = True
                raise RuntimeError(f"OddsPapi v4 error on {path}: "
                                   f"{err.get('message')} ({err.get('code')})")
            self.quota_exhausted = False
            return data
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _retry_delay(resp: httpx.Response) -> float:
        try:
            retry_ms = (resp.json().get("error") or {}).get("retryMs")
            if retry_ms:
                return float(retry_ms) / 1000.0 + 0.1
        except Exception:
            pass
        return 1.1

    async def get_fixtures(self, start_from_epoch: int, start_to_epoch: int,
                           sport_id: int = TENNIS_SPORT_ID) -> list[dict]:
        data = await self._get("/fixtures", {
            "sportId": sport_id,
            "from": _to_iso(start_from_epoch),
            "to": _to_iso(start_to_epoch),
        })
        return data if isinstance(data, list) else []

    async def get_odds_by_tournaments(self, bookmaker: str, tournament_ids: list[int],
                                      chunk_size: int = TOURNAMENT_CHUNK_SIZE) -> list[dict]:
        if not tournament_ids:
            return []
        chunk_size = min(chunk_size, _BOOKMAKER_CHUNK_LIMITS.get(bookmaker, chunk_size))
        fixtures: list[dict] = []
        for i in range(0, len(tournament_ids), chunk_size):
            chunk = tournament_ids[i:i + chunk_size]
            try:
                data = await self._get("/odds-by-tournaments", {
                    "bookmaker": bookmaker,
                    "tournamentIds": ",".join(str(t) for t in chunk),
                })
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue
                raise
            if isinstance(data, list):
                fixtures.extend(data)
        return fixtures

    async def get_pinnacle_matches(self, sport: str, start_epoch: int, end_epoch: int,
                                   tournament_filter=None) -> list[dict]:
        """Normalized Pinnacle matches (H2H + main total) for a sport in the window.

        tournament_filter: optional list of lowercase substrings; only tournaments
        whose name contains one are kept (e.g. ["nba", "wnba", "eurobasket"]).
        """
        if self.use_mock:
            return mock_data.build_mock_pinnacle_matches(sport, start_epoch, end_epoch)

        sport_id = SPORT_IDS.get(sport, TENNIS_SPORT_ID)
        window: list[dict] = []
        for fx in await self.get_fixtures(start_epoch, end_epoch, sport_id):
            if not fx.get("hasOdds") or not _is_real_event(fx):
                continue
            if not _matches_whitelist(fx, tournament_filter):
                continue
            st = _iso_epoch(fx.get("startTime"))
            if st is None or not (start_epoch < st <= end_epoch):
                continue
            fx["_startEpoch"] = st
            window.append(fx)

        ids = sorted({fx["tournamentId"] for fx in window})
        pin = await self.get_odds_by_tournaments(SHARP_BOOK, ids)
        pin_by_id = {f["fixtureId"]: f for f in pin}

        out: list[dict] = []
        for fx in window:
            pf = pin_by_id.get(fx["fixtureId"])
            book = _single_book_odds(pf) if pf else None
            if not book or book.get("suspended"):
                continue
            p1 = fx.get("participant1Name") or "1"
            p2 = fx.get("participant2Name") or "2"
            selections: list[dict] = []
            h2h = _pinnacle_h2h(book)
            if h2h:
                selections.append({"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                                   "outcome": "home", "point": None, "label": p1,
                                   "price": h2h["home"]})
                selections.append({"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                                   "outcome": "away", "point": None, "label": p2,
                                   "price": h2h["away"]})
                if "draw" in h2h:
                    selections.append({"market_key": "h2h", "market_name": H2H_MARKET_NAME,
                                       "outcome": "draw", "point": None, "label": "Pareggio",
                                       "price": h2h["draw"]})
            total = _pinnacle_main_total(book)
            if total:
                selections.append({"market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                                   "outcome": "Over", "point": total["line"],
                                   "label": f"Over {total['line']}", "price": total["over_price"]})
                selections.append({"market_key": "totals", "market_name": TOTALS_MARKET_NAME,
                                   "outcome": "Under", "point": total["line"],
                                   "label": f"Under {total['line']}", "price": total["under_price"]})
            if selections:
                out.append({
                    "match_id": fx["fixtureId"],
                    "tournament": fx.get("tournamentName"),
                    "player1": p1,
                    "player2": p2,
                    "start_epoch": fx["_startEpoch"],
                    "selections": selections,
                })
        return out
