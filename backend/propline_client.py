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
import re
import time
import unicodedata
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

# ---- Best Italian price (alert-time cross-check) ----
# Italian ADM-licensed books PropLine carries (soccer 1X2 / O-U ladder and
# tennis singles, pregame), verified live on /v1/freshness.
ITALY_BOOKS = {"snai": "Snai", "eurobet": "Eurobet", "admiralbet": "AdmiralBet"}
# OddsPapi tournament-name substring -> PropLine sport key. First match wins, so
# "brasileiro serie a" must precede the generic "serie a" (Italy). Anything not
# listed is resolved by title against /v1/sports (see _sport_key_for).
SOCCER_SPORT_KEYS = [
    ("brasileiro serie a", "soccer_brasileirao"),
    ("liga profesional", "soccer_argentina_primera"),
    ("liga mx", "soccer_liga_mx"),
    ("uefa nations league", "soccer_uefa_nations_league"),
    ("premier league", "soccer_epl"),
    ("laliga", "soccer_la_liga"),
    ("la liga", "soccer_la_liga"),
    ("serie a", "soccer_serie_a"),
    ("bundesliga", "soccer_bundesliga"),
    ("ligue 1", "soccer_ligue_1"),
    ("eredivisie", "soccer_eredivisie"),
    ("liga portugal", "soccer_primeira_liga"),
    # UEFA cups (keys verified live on /v1/sports, 2026-10-24); "women" editions
    # never reach here because the women's tournaments aren't on PropLine.
    ("champions league", "soccer_uefa_champions_league"),
    ("europa league", "soccer_uefa_europa_league"),
    ("conference league", "soccer_uefa_conference_league"),
]
XCHECK_TTL = 60.0              # share one odds read between alerts of a scan
SPORTS_LIST_TTL = 24 * 3600.0  # /v1/sports barely changes
# How far the PropLine kickoff may sit from the provider's start time.
# Tennis start times are loose (order-of-play), football ones are exact.
START_TOLERANCE = {"football": 30 * 60, "tennis": 3 * 3600}


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


def _norm(s) -> str:
    """Lowercase, accent-free, punctuation-free; OddsPapi's 'Last, First' is
    turned into 'First Last' so tennis names line up across providers."""
    s = str(s or "").strip().lower()
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


_GENERIC = {"fc", "cf", "ac", "sc", "afc", "cd", "ud", "sd", "fk", "sk", "ss", "us",
            "as", "club", "de", "the", "real", "city", "united", "calcio", "1"}


def _same_side(a, b, sport: str) -> bool:
    """Same team/player across providers. Tennis: equal names or equal surname
    (last word). Football: equal names, one contained in the other, or a shared
    distinctive word (OddsPapi short names vs bookmaker full names)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if sport == "tennis":
        return na.split()[-1] == nb.split()[-1]
    if na in nb or nb in na:
        return True
    wa = {w for w in na.split() if len(w) >= 3 and w not in _GENERIC}
    wb = {w for w in nb.split() if len(w) >= 3 and w not in _GENERIC}
    return bool(wa & wb)


def italian_prices(ev: dict, home: str, away: str, market_key: str, outcome: str,
                   point, sport: str) -> list[dict]:
    """[{bookmaker, price}] from the Italian books for one selection of the
    event, best first. `outcome`: home/away/draw for h2h, over/under for totals
    (the provider's orientation; flipped if PropLine lists the teams the other
    way round)."""
    flipped = (_same_side(home, ev.get("away_team"), sport)
               and _same_side(away, ev.get("home_team"), sport)
               and not _same_side(home, ev.get("home_team"), sport))
    want = outcome.lower()
    if market_key == "h2h" and flipped and want in ("home", "away"):
        want = "away" if want == "home" else "home"
    team_name = {"home": ev.get("home_team"), "away": ev.get("away_team")}.get(want)
    prices: list[dict] = []
    for b in ev.get("bookmakers") or []:
        label = ITALY_BOOKS.get(b.get("key"))
        if not label:
            continue
        for mk in b.get("markets") or []:
            if mk.get("key") != market_key or mk.get("period") or mk.get("team"):
                continue  # period lines and team totals share the key
            for o in mk.get("outcomes") or []:
                name = str(o.get("name") or "")
                if market_key == "h2h":
                    side = o.get("side")
                    if side:
                        ok = side == want
                    elif want == "draw":
                        ok = name.lower() in ("draw", "x", "pareggio")
                    else:
                        ok = bool(team_name) and _same_side(name, team_name, sport)
                else:
                    try:
                        ok = (name.lower() == want and point is not None
                              and abs(float(o.get("point")) - float(point)) < 1e-9)
                    except (TypeError, ValueError):
                        ok = False
                if not ok:
                    continue
                dec = _outcome_decimal(o)
                if dec:
                    prices.append({"bookmaker": label, "price": dec})
                break
    best: dict[str, float] = {}
    for p in prices:  # one price per book (its best if it lists duplicates)
        best[p["bookmaker"]] = max(best.get(p["bookmaker"], 0.0), p["price"])
    return sorted(({"bookmaker": k, "price": v} for k, v in best.items()),
                  key=lambda x: -x["price"])


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
            # Every book's price for this exact prop (source included), keyed
            # by book: the monitor's multi-book steam rule tracks each one.
            book_prices = {bk: props[(mkey, player, point, side)]
                           for bk, props in books.items()
                           if (mkey, player, point, side) in props}
            label_side = "Over" if side == "over" else "Under"
            selections.append({
                "market_key": f"prop:{mkey}:{player}",
                "market_name": f"{PROP_MARKETS[mkey]} {player}",
                "outcome": side,
                "point": point,
                "label": f"{label_side} {point:g}",
                "price": price,
                "others": others,
                "book_prices": book_prices,
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
        self._sports_list: tuple[float, list[dict]] | None = None
        # sport_key -> (monotonic fetch time, Italian-book events)
        self._it_cache: dict[str, tuple[float, list[dict]]] = {}

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

    async def _sport_key_for(self, sport: str, tournament: str | None) -> str | None:
        """PropLine sport key for a provider match: 'tennis' for tennis; for
        football the SOCCER_SPORT_KEYS table, else a title match against
        /v1/sports (cached a day) - that is how the UEFA cups resolve."""
        if sport == "tennis":
            return "tennis"
        if sport != "football":
            return None
        t = (tournament or "").lower()
        for pat, key in SOCCER_SPORT_KEYS:
            if pat in t:
                return key
        now = time.monotonic()
        if self._sports_list is None or now - self._sports_list[0] >= SPORTS_LIST_TTL:
            data = await self._get("/sports")
            self._sports_list = (now, data if isinstance(data, list) else [])
        tn = _norm(t)
        for s in self._sports_list[1]:
            key = str(s.get("key") or "")
            title = _norm(re.sub(r"\(.*?\)", "", str(s.get("title") or "")))
            if not key.startswith("soccer_") or "women" in title or len(title) < 6:
                continue
            if title in tn or tn in title:
                return key
        return None

    async def _italian_odds(self, sport_key: str) -> list[dict]:
        now = time.monotonic()
        hit = self._it_cache.get(sport_key)
        if hit is not None and now - hit[0] < XCHECK_TTL:
            return hit[1]
        data = await self._get(f"/sports/{sport_key}/odds", {
            "markets": "h2h,totals", "bookmakers": ",".join(ITALY_BOOKS)})
        events = data if isinstance(data, list) else (data or {}).get("data") or []
        self._it_cache[sport_key] = (now, events)
        return events

    async def best_italian_price(self, sport: str, tournament: str | None,
                                 start_epoch: int, home: str, away: str,
                                 market_key: str, outcome: str, point) -> list[dict] | None:
        """Italian-book prices (Snai/Eurobet/AdmiralBet, best first) for the
        alerted selection, or None. One bulk request per league, shared by
        every alert of that league within XCHECK_TTL."""
        if (not self.api_key or self.use_mock or self.quota_exhausted
                or market_key not in ("h2h", "totals")):
            return None
        sport_key = await self._sport_key_for(sport, tournament)
        if sport_key is None:
            return None
        tol = START_TOLERANCE.get(sport, 30 * 60)
        for ev in await self._italian_odds(sport_key):
            st = _iso_epoch(ev.get("commence_time"))
            if st is None or abs(st - start_epoch) > tol:
                continue
            straight = (_same_side(home, ev.get("home_team"), sport)
                        and _same_side(away, ev.get("away_team"), sport))
            swapped = (_same_side(home, ev.get("away_team"), sport)
                       and _same_side(away, ev.get("home_team"), sport))
            if not (straight or swapped):
                continue
            prices = italian_prices(ev, home, away, market_key, outcome, point, sport)
            return prices or None
        return None

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
