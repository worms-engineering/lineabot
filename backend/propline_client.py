"""PropLine provider - NBA and Serie A player props from the sportsbooks.

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
import statistics
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

import mock_data

logger = logging.getLogger(__name__)

BASE_URL = "https://api.prop-line.com/v1"
SPORT_KEYS = {"props_nba": "basketball_nba", "props_seriea": "soccer_serie_a",
              "props_epl": "soccer_epl"}
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
    "thescorebet": "theScore", "novig": "Novig",
}
# Only track sides priced inside this decimal band: main prop lines sit around
# 1.70-2.30, and far-off prices are thin ladders where one tick is a big "move".
PRICE_BAND = (1.25, 5.0)
# Schedules move rarely; an hour keeps the fixed cost at ~24 requests/day/sport.
SCHEDULE_TTL = 60 * 60

# Serie A player props. No Pinnacle and no Italian book prices them on PropLine
# (verified live), so it's the US books. BetRivers stands for the Kambi group:
# Unibet / BetRivers / Hard Rock post identical prices (one feed), and asking
# for all three would count one opinion three times in the steam rule.
# Goalscorer markets (anytime / first / 2+) are deliberately left out: the user
# doesn't want alerts on them, so they aren't even requested.
SOCCER_PROP_MARKETS = {
    "player_shots_on_target": "Tiri in porta",
    "player_shots": "Tiri",
}
SOCCER_PROP_BOOKS = ["fanduel", "draftkings", "bovada", "fanatics", "thescorebet",
                     "betmgm", "novig", "betrivers"]

# Per-sport settings. `universe`: which lines become selections (see
# parse_props_events); `rungs`: also read N+ ladders / YES-only lists;
# `min_interval`: seconds between two odds requests (the fast loop runs every
# 60s). Props are only polled in the hour before kick-off, so even one request
# per league per minute costs ~55 per match slot - well inside the free quota.
PROP_SPORTS = {
    "props_nba": {"label": "NBA", "markets": PROP_MARKETS,
                  "books": [SOURCE_BOOK] + [b for b in CONTEXT_BOOKS if b != SOURCE_BOOK],
                  "universe": "source", "rungs": False, "band": PRICE_BAND,
                  "min_interval": 0},
    "props_seriea": {"label": "Serie A", "markets": SOCCER_PROP_MARKETS,
                     "books": SOCCER_PROP_BOOKS, "universe": "union", "rungs": True,
                     # scorer prices run longer than NBA lines; beyond ~6.0 one
                     # ladder tick is already a big % move
                     "band": (1.25, 6.0),
                     "min_interval": int(os.environ.get("PROPLINE_SOCCER_INTERVAL", "60"))},
}
# Premier League: same books / markets / cadence as Serie A.
PROP_SPORTS["props_epl"] = {**PROP_SPORTS["props_seriea"], "label": "Premier League"}
# Daily-quota guard: below this many requests left, the football props (one
# request per league per poll) pause until the 00:00 UTC reset, so the NBA
# props and the alert-time Italian-price lookups keep working all day.
SOCCER_PROPS_RESERVE = int(os.environ.get("PROPLINE_SOCCER_RESERVE", "150"))

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


_RUNG = re.compile(r"^\s*(\d+)\s*\+")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _book_props(bookmaker: dict, markets: dict, rungs: bool) -> dict[tuple, tuple]:
    """{(market, player_key, point, side): (decimal price, display name)} for
    one book's player props, normalized so the same bet lines up across books:
    - two-way Over/Under (or Yes/No) with a line -> (point, over|under);
    - milestone rung "N+ ..." -> Over N-0.5 (e.g. "2+ Shots On Target" is the
      same bet as Over 1.5);
    - YES-only player list (anytime scorer, "to have a shot on target") ->
      Over 0.5, i.e. 1+.
    `rungs=False` keeps only the two-way lines (NBA, where every book posts
    them). The player is keyed by PropLine's cross-book `player_id` when it is
    known, else by the normalized name (Bovada's "(BOL)" team tag stripped)."""
    out: dict[tuple, tuple] = {}
    for mk in bookmaker.get("markets") or []:
        key = mk.get("key")
        if key not in markets or mk.get("period"):
            continue
        for o in mk.get("outcomes") or []:
            name = str(o.get("name") or "").strip()
            n = name.lower()
            point = o.get("point")
            display = _PAREN.sub("", str(o.get("description") or "").strip())
            if point is not None and n in ("over", "under", "yes", "no"):
                side = "over" if n in ("over", "yes") else "under"
                pt = float(point)
            elif not rungs:
                continue
            elif _RUNG.match(name):
                side, pt = "over", int(_RUNG.match(name).group(1)) - 0.5
            elif n in ("yes", "no"):
                side, pt = ("over" if n == "yes" else "under"), 0.5
            else:  # YES-only list: the outcome name is the player
                side, pt = "over", 0.5
                display = display or _PAREN.sub("", name)
            if not display:
                continue
            pkey = o.get("player_id") or _norm(display)
            dec = _outcome_decimal(o)
            if dec:
                out[(key, pkey, pt, side)] = (dec, display)
    return out


def _side_label(mkey: str, point: float, side: str) -> str:
    if mkey == "anytime_goal_scorer" and point == 0.5:
        return "Segna" if side == "over" else "Non segna"
    return f"{'Over' if side == 'over' else 'Under'} {point:g}"


def parse_props_events(events: list, start_epoch: int, end_epoch: int,
                       source_book: str = SOURCE_BOOK,
                       sport: str = "props_nba") -> list[dict]:
    """Normalize a bulk /odds payload into monitor matches: one match per game
    starting in (start_epoch, end_epoch], one selection per prop side, each
    carrying every book's price for it (`book_prices`, for the multi-book
    steam rule) and the non-source books' prices for the alert (`others`).

    NBA ("source" universe): the lines the source book posts, both sides
    required. Serie A ("union" universe): every line quoted by at least two
    books - the books cover different players, and the steam rule needs two
    anyway."""
    conf = PROP_SPORTS[sport]
    markets = conf["markets"]
    band = conf["band"]
    out: list[dict] = []
    for ev in events or []:
        if ev.get("live"):
            continue
        st = _iso_epoch(ev.get("commence_time"))
        if st is None or not (start_epoch < st <= end_epoch):
            continue
        books = {b.get("key"): _book_props(b, markets, conf["rungs"])
                 for b in ev.get("bookmakers") or []}
        src = books.get(source_book) or {}
        if conf["universe"] == "source":
            keys = [k for k in src
                    if (k[0], k[1], k[2], "under" if k[3] == "over" else "over") in src]
        else:
            count: dict[tuple, int] = {}
            for props in books.values():
                for k in props:
                    count[k] = count.get(k, 0) + 1
            keys = [k for k, c in count.items() if c >= 2]
        selections: list[dict] = []
        for k in keys:
            mkey, pkey, point, side = k
            book_prices = {bk: props[k][0] for bk, props in books.items() if k in props}
            price = (src[k][0] if k in src
                     else statistics.median(list(book_prices.values())))
            if not (band[0] <= price <= band[1]):
                continue
            display = (src.get(k) or next(props[k] for props in books.values() if k in props))[1]
            others = [{"book": _book_label(bk), "price": p}
                      for bk, p in book_prices.items() if bk != source_book]
            selections.append({
                "market_key": f"prop:{mkey}:{pkey}",
                "market_name": f"{markets[mkey]} {display}",
                "outcome": side,
                "point": point,
                "label": _side_label(mkey, point, side),
                "price": round(price, 3),
                "others": others,
                "book_prices": book_prices,
                "player": display,
            })
        if selections:
            out.append({
                "match_id": f"pl-{ev.get('id')}",
                "tournament": f"{conf['label']} · Player props",
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
        self.daily_limit: int | None = None  # X-Daily-Limit (plan's daily cap)
        self.quota_exhausted = False
        self.ip_blocked = False
        # sport_key -> (monotonic fetch time, [commence epochs])
        self._schedule: dict[str, tuple[float, list[int]]] = {}
        self._warned_no_key = False
        # Last request outcome, surfaced in the dashboard (the props loop runs in
        # the background, so a rejected key would otherwise go unnoticed).
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self._sports_list: tuple[float, list[dict]] | None = None
        # sport -> monotonic time of its last odds request (per-sport cadence)
        self._last_fetch: dict[str, float] = {}
        # sport_key -> (monotonic fetch time, Italian-book events)
        self._it_cache: dict[str, tuple[float, list[dict]]] = {}

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("PropLine key not configured (PROPLINE_API_KEY)")
        try:
            resp = await self._client.get(f"{BASE_URL}{path}", params=params or {},
                                          headers={"X-API-Key": self.api_key})
        except Exception as e:
            self.last_error = f"PropLine non raggiungibile ({type(e).__name__})"
            raise
        for attr, header in (("requests_remaining", "X-Daily-Remaining"),
                             ("daily_limit", "X-Daily-Limit")):
            val = resp.headers.get(header)
            if val is not None:
                try:
                    setattr(self, attr, int(val))
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
                self.last_error = "quota giornaliera esaurita (reset alle 00:00 UTC)"
            raise RuntimeError(f"PropLine 429 on {path}: {body.get('error') or resp.text[:120]}")
        if resp.status_code in (401, 403):
            self.last_error = f"key rifiutata da PropLine ({resp.status_code})"
            raise RuntimeError(f"PropLine rejected the API key ({resp.status_code})")
        if resp.status_code >= 400:
            self.last_error = f"errore PropLine HTTP {resp.status_code}"
        resp.raise_for_status()
        self.quota_exhausted = (self.requests_remaining is not None
                                and self.requests_remaining <= 0)
        self.last_error = None
        self.last_ok_at = time.time()
        return resp.json()

    async def check_key(self) -> dict:
        """One cheap authenticated request (/v1/sports, 1 of the daily quota)
        to tell the dashboard whether the current key works right now."""
        if not self.api_key:
            return {"ok": False, "error": "nessuna key impostata", "remaining": None}
        try:
            await self._get("/sports")
            return {"ok": True, "error": None, "remaining": self.requests_remaining}
        except Exception as e:
            return {"ok": False, "error": self.last_error or str(e)[:120],
                    "remaining": self.requests_remaining}

    def status_text(self) -> str | None:
        """Human status for the Settings dialog, from the last request."""
        if not self.api_key:
            return None
        if self.last_error:
            return f"✗ {self.last_error}"
        if self.last_ok_at is not None:
            left = (f" · {self.requests_remaining} richieste rimaste oggi"
                    if self.requests_remaining is not None else "")
            return f"✓ key valida{left}"
        return "non ancora verificata"

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
                                   tournament_filter=None) -> list[dict] | None:
        """Props matches for the window, [] when nothing is in it, or None when
        this sport's own cadence (min_interval) says not to poll yet - the
        monitor then keeps the previous snapshot instead of blanking it."""
        sport_key = SPORT_KEYS.get(sport)
        conf = PROP_SPORTS.get(sport)
        if sport_key is None or conf is None:
            return []
        if self.use_mock:
            return mock_data.build_mock_pinnacle_matches("basketball", start_epoch, end_epoch)
        if not self.api_key:
            if not self._warned_no_key:
                logger.info("PropLine key not set: book props are idle")
                self._warned_no_key = True
            return []
        if not any(start_epoch < t <= end_epoch for t in await self._game_times(sport_key)):
            return []  # no game in the window: don't spend a request
        now = time.monotonic()
        last = self._last_fetch.get(sport)
        if last is not None and now - last < conf["min_interval"]:
            return None
        if (sport != "props_nba" and self.requests_remaining is not None
                and self.requests_remaining < SOCCER_PROPS_RESERVE):
            return None  # keep the rest of today's quota for NBA + Italian prices
        self._last_fetch[sport] = now
        data = await self._get(f"/sports/{sport_key}/odds", {
            "markets": ",".join(conf["markets"]), "bookmakers": ",".join(conf["books"])})
        events = data if isinstance(data, list) else (data or {}).get("data") or []
        return parse_props_events(events, start_epoch, end_epoch, sport=sport)
