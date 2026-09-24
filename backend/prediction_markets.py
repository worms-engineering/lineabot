"""Prediction-market cross-checks (Polymarket, Kalshi) for drop alerts.

Both platforms expose keyless, quota-free read-only APIs - unlike the odds
providers, these cost nothing per lookup:
- Polymarket Gamma: game events titled "TeamA vs. TeamB"; the market carries
  `outcomes` (["TeamA","TeamB"]) and `outcomePrices` (probabilities 0-1).
- Kalshi: per-game binary markets grouped by ticker prefix
  (KXWNBAGAME-26AUG25PDXDAL-PDX / -DAL), titles "<Team> wins",
  yes_ask_dollars/yes_bid_dollars (0-1 dollar strings).

Prices are probabilities: decimal odds = 1 / probability. Liquidity is thin
far from tip-off and quotes can be absent (no ask/bid), so every lookup
returns None liberally - a missing cross-check must never break an alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
_KALSHI_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"

# One shared, keep-alive HTTP client for all Polymarket/Kalshi calls. These run
# very frequently (the fast loop every minute, 8 outright tags per main scan,
# plus alert cross-checks), and opening a fresh AsyncClient per call paid a full
# TLS handshake each time. Created lazily inside the event loop; reused via the
# `_client()` context manager, which yields it WITHOUT closing it on exit.
_HTTP: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None or _HTTP.is_closed:
        _HTTP = httpx.AsyncClient(
            timeout=25.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _HTTP


@asynccontextmanager
async def _client():
    """Yield the shared client; does NOT close it (unlike `async with
    httpx.AsyncClient()`), so connections stay pooled across calls."""
    yield _get_http()


async def _close_http() -> None:
    global _HTTP
    if _HTTP is not None and not _HTTP.is_closed:
        await _HTTP.aclose()
    _HTTP = None


# Gamma /events silently caps a page at 100 events whatever `limit` asks for,
# so a single request only ever saw the first 100 events of a tag - e.g. ~100 of
# the ~330 open MLB events, missing most games in the window. Every read now
# pages with offset (the API refuses offsets past ~2000, hence the page cap).
_GAMMA_PAGE = 100
_GAMMA_MAX_PAGES = 10


async def _gamma_events(tag: str, max_pages: int = _GAMMA_MAX_PAGES,
                        **filters) -> list[dict]:
    """All open events of a Polymarket tag, paginated. Extra `filters` go
    straight to the query (e.g. start_time_min/start_time_max to fetch only the
    games starting in a window). Raises on transport errors; a non-200 page
    ends the read with what was collected so far."""
    out: list[dict] = []
    client = _get_http()
    for page in range(max_pages):
        r = await client.get(_GAMMA_EVENTS, params={
            "limit": _GAMMA_PAGE, "offset": page * _GAMMA_PAGE,
            "active": "true", "closed": "false", "tag_slug": tag, **filters})
        if r.status_code != 200:
            logger.warning("gamma events tag=%s page=%d -> HTTP %s", tag, page, r.status_code)
            break
        events = r.json()
        if not isinstance(events, list):
            break
        out.extend(events)
        if len(events) < _GAMMA_PAGE:
            break
    return out


# Short per-tag cache for the alert-time cross-check only: several alerts in
# one scan (or a burst of scans) share one read of a tag. Never used for the
# prices that are tracked for drops - those are always read live.
_XCHECK_CACHE_TTL = 60.0
_xcheck_cache: dict[str, tuple[float, list[dict]]] = {}


async def _gamma_events_xcheck(tag: str) -> list[dict]:
    now = asyncio.get_running_loop().time()
    hit = _xcheck_cache.get(tag)
    if hit is not None and now - hit[0] < _XCHECK_CACHE_TTL:
        return hit[1]
    events = await _gamma_events(tag, max_pages=5)
    _xcheck_cache[tag] = (now, events)
    return events


# sport -> Polymarket tag slugs carrying game moneylines.
POLYMARKET_TAGS = {
    "basketball": ["nba", "wnba", "euroleague-basketball"],
    "football": ["epl", "ucl", "uefa-nations-league"],
    # Per-match tennis moneylines; outcomes are last names, matched via
    # word overlap; stale/resolved events are rejected by the time gate
    # and the 2%-97% probability band.
    "tennis": ["tennis"],
}
# sport -> Kalshi game-winner series tickers. Basketball only for now:
# football titles are city-based and error-prone to match.
KALSHI_SERIES = {
    "basketball": ["KXNBAGAME", "KXWNBAGAME", "KXEUROLEAGUEGAME"],
}
# Max |Kalshi event time - game start| for a game market to count as the same
# game (see _kalshi_event_ts).
_KALSHI_TIME_TOLERANCE = 36 * 3600

# ---- F1 tracking (Polymarket as source, Kalshi as cross-check) ----
# Polymarket event-title suffixes worth tracking; the rest (practice,
# qualifying, fastest lap, props) is noise with no liquidity.
F1_EVENT_MARKETS = [
    ("Driver Winner", "winner", "Race Winner"),
    ("Driver Podium Finish", "podium", "Podium"),
    ("Head-to-Head", "h2h", "Head-to-Head"),
]
# Only track selections priced inside this probability band: outside it the
# decimal odds are unusable (2000.0) and the spread dwarfs any signal.
F1_PROB_BAND = (0.02, 0.97)

_F1_YES_Q = re.compile(r"^Will\s+(.+?)\s+(?:win|finish|get|achieve)\b", re.I)
_F1_H2H_Q = re.compile(r"^Who will finish higher:\s*(.+?)\s+or\s+(.+?)\?\s*$", re.I)

# Outright (tournament-winner / award) markets: one contender per binary
# "Will <name> win ...?" sub-market. Used by get_outright_matches.
_OUTRIGHT_WIN_Q = re.compile(r"^Will\s+(.+?)\s+win\b", re.I)
# Polymarket seeds outright fields with generic placeholder contenders ("Team A",
# "Another Team", "The Field", ...) parked at 0.50 until the slot is assigned -
# never a real bet, and a resolving placeholder would read as a huge phantom
# drop, so skip them.
_OUTRIGHT_PLACEHOLDER = re.compile(
    r"^(team [a-z0-9]+|another team|the field|field|other|any other)\b", re.I)
# Only track contenders inside this probability band (outside it the decimal
# odds are meaningless and it's the longshot tail of a big field).
OUTRIGHT_PROB_BAND = (0.02, 0.95)
# Skip thin outright markets - a shallow book makes the % swings noise.
OUTRIGHT_MIN_LIQUIDITY = 100_000.0

# Reject markets with no real order book. A Polymarket market with ~0 current
# liquidity has no depth to trade against, so its "price" is a stale last trade
# (or a resolved 1/0) and unreliable - exactly the markets that produced bogus
# odds. Require at least this much book liquidity to trust the price. Tunable.
PM_MIN_LIQUIDITY = float(os.environ.get("PM_MIN_LIQUIDITY", "500"))


def _market_liquidity(m: dict) -> float:
    """Current order-book liquidity (USD) of a Polymarket market; 0 if absent."""
    v = m.get("liquidityNum")
    if v is None:
        v = m.get("liquidity")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---- Whale monitor -------------------------------------------------------
# Detect large single trades ("whales") on Polymarket via the keyless Data API,
# which supports a server-side cash-size filter so one call returns only the big
# recent trades platform-wide; we then keep those whose market belongs to a sport
# we watch (matched by conditionId against a cached tag->market map).
_DATA_TRADES = "https://data-api.polymarket.com/trades"
# Initial seed only; the live threshold lives on the monitor (whale_min_usd) and
# is adjustable from the dashboard. Env var is the default until one is saved.
WHALE_MIN_USD = float(os.environ.get("WHALE_MIN_USD", "25000"))
# Polymarket tag slugs whose markets we watch for whale trades: the user's
# Pinnacle sports (football/tennis/basket) + the prediction sports + outrights.
# Football uses the per-league tags of the tracked competitions: the catch-all
# `soccer` tag holds 2000+ open events (~180 MB, past the API's offset cap), so
# it can't be read in full - and it's mostly leagues we don't track anyway.
# Slugs verified live against Gamma.
_FOOTBALL_WHALE_TAGS = [
    "epl", "la-liga", "serie-a", "bundesliga", "ligue-1", "ere", "primeira-liga",
    "brazil-serie-a", "arg", "mex", "ucl", "champions-league", "uel",
    "europa-league", "uefa-conference-league", "europa-conference-league",
    "uefa-nations-league",
]
WHALE_TAGS = _FOOTBALL_WHALE_TAGS + ["tennis", "nba", "basketball", "wnba",
                                     "f1", "mlb", "golf"]
# tag slug -> (emoji, label) for the alert.
_WHALE_TAG_SPORT = {
    **{t: ("⚽", "Calcio") for t in _FOOTBALL_WHALE_TAGS},
    "tennis": ("🎾", "Tennis"), "nba": ("🏀", "Basket"),
    "basketball": ("🏀", "Basket"), "wnba": ("🏀", "Basket"),
    "f1": ("🏎️", "Formula 1"), "mlb": ("⚾", "MLB"), "golf": ("⛳", "Golf"),
}

# Words too generic to identify a team on their own ("Manchester United" vs
# "Manchester City" share "manchester"; "City"/"United" match half of England).
_GENERIC_WORDS = {"city", "united", "fc", "ac", "sc", "real", "athletic",
                  "sporting", "hotspur", "fc.", "los", "las", "the", "de"}

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _norm(s) -> str:
    return " ".join(_WORD_SPLIT.split(str(s or "").lower())).strip()


def _words(name) -> set[str]:
    return {w for w in _WORD_SPLIT.split(str(name or "").lower())
            if len(w) >= 3 and w not in _GENERIC_WORDS}


def _same_team(a, b, exclude: set[str] = frozenset()) -> bool:
    """Fuzzy same-team: exact normalized equality, or an overlap of
    distinctive words (OddsPapi nicknames vs platform city/full names).
    `exclude` drops words shared by BOTH teams of the fixture (e.g. the
    "manchester" in United vs City) so city-sharing clubs can't cross-match."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa = _words(a) - exclude
    wb = _words(b) - exclude
    return bool(wa & wb)


def _shared_words(home, away) -> set[str]:
    return _words(home) & _words(away)


def _to_decimal(prob) -> float | None:
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    if not 0.01 <= p <= 0.99:  # unpriced or fully resolved
        return None
    return round(1.0 / p, 3)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(v) -> int | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


async def polymarket_price(sport: str, home: str, away: str,
                           start_epoch: int, side: str) -> float | None:
    """Decimal odds for `side` to win the game, or None."""
    tags = POLYMARKET_TAGS.get(sport)
    if not tags or not side:
        return None
    shared = _shared_words(home, away)
    for tag in tags:
        try:
            events = await _gamma_events_xcheck(tag)
        except Exception:
            continue
        for e in events:
            # NB: event.startDate is the LISTING date; the game time is
            # market.gameStartTime (or event.endDate as fallback). A
            # time gate is required or stale same-matchup events match.
            title_teams = [_norm(t) for t in
                           re.split(r"\s+vs\.?\s+", str(e.get("title") or ""), flags=re.I)]
            if len(title_teams) != 2:
                continue
            if not (_same_team(title_teams[0], home, shared) and _same_team(title_teams[1], away, shared)) \
                    and not (_same_team(title_teams[0], away, shared) and _same_team(title_teams[1], home, shared)):
                continue
            for m in e.get("markets") or []:
                game_ts = (_parse_ts(m.get("gameStartTime"))
                           or _parse_ts(m.get("closeTime"))
                           or _parse_ts(e.get("endDate")))
                if game_ts is None or abs(game_ts - start_epoch) > 36 * 3600:
                    continue
                try:
                    outcomes = m.get("outcomes")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    prices = m.get("outcomePrices")
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                except Exception:
                    continue
                if not outcomes or not prices or len(outcomes) != len(prices):
                    continue
                for name, p in zip(outcomes, prices):
                    if _same_team(name, side, shared):
                        price = _to_decimal(p)
                        if price:
                            logger.info("polymarket %s vs %s: %s @ %s",
                                        home, away, side, price)
                        return price
    return None


def _kalshi_event_ts(m: dict) -> int | None:
    """When the event actually happens. `occurrence_datetime` /
    `expected_expiration_time` sit ~3h after tip-off (~6h after an F1 start);
    `close_time` is only the trading deadline, ~2 days to a week later - gating
    on it (as before) rejected every NBA/WNBA/F1 market against the 36h/72h
    windows."""
    return (_parse_ts(m.get("occurrence_datetime"))
            or _parse_ts(m.get("expected_expiration_time"))
            or _parse_ts(m.get("close_time")))


def _kalshi_yes_prob(m: dict) -> float | None:
    """YES price of a Kalshi market as a 0-1 probability (ask, else bid).
    Kalshi now publishes prices as dollar strings (`yes_ask_dollars`, e.g.
    "0.4100"); the legacy integer-cent fields (`yes_ask`) come back null, which
    silently disabled every Kalshi cross-check. Cents kept as a fallback."""
    for key in ("yes_ask_dollars", "yes_bid_dollars"):
        try:
            v = float(m.get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    cents = m.get("yes_ask") or m.get("yes_bid")
    return cents / 100 if cents else None


async def kalshi_price(sport: str, home: str, away: str,
                       start_epoch: int, side: str) -> float | None:
    """Decimal odds for `side` to win the game, or None."""
    series_list = KALSHI_SERIES.get(sport)
    if not series_list or not side:
        return None
    shared = _shared_words(home, away)
    async with _client() as client:
        for series in series_list:
            try:
                r = await client.get(_KALSHI_MARKETS, params={
                    "series_ticker": series, "status": "open", "limit": 200})
                data = r.json() if r.status_code == 200 else {}
            except Exception:
                continue
            # Group the two "<Team> wins" markets of a game by ticker prefix.
            groups: dict[str, list[dict]] = {}
            for m in (data or {}).get("markets") or []:
                ticker = str(m.get("ticker") or "")
                prefix = ticker.rsplit("-", 1)[0] if ticker else ""
                if prefix:
                    groups.setdefault(prefix, []).append(m)
            win_title = re.compile(r"^(.*?)\s+wins$", re.I)
            for ms in groups.values():
                sides = {}
                ok_time = False
                for m in ms:
                    mt = win_title.match(str(m.get("title") or ""))
                    if not mt:
                        continue
                    sides[mt.group(1)] = m
                    ev_ts = _kalshi_event_ts(m)
                    if ev_ts is not None and abs(ev_ts - start_epoch) <= _KALSHI_TIME_TOLERANCE:
                        ok_time = True
                if len(sides) != 2 or not ok_time:
                    continue
                names = list(sides)
                if not ((_same_team(names[0], home, shared) and _same_team(names[1], away, shared))
                        or (_same_team(names[0], away, shared) and _same_team(names[1], home, shared))):
                    continue
                for name, m in sides.items():
                    if _same_team(name, side, shared):
                        prob = _kalshi_yes_prob(m)
                        price = _to_decimal(prob) if prob else None
                        if price:
                            logger.info("kalshi %s: %s @ %s", series, side, price)
                        return price
    return None


_KALSHI_F1_Q = re.compile(
    r"^(?:Will\s+(.+?)\s+win\b|(.+?)\s+to\s+finish\s+in\s+first\b)", re.I)


async def kalshi_f1_price(driver: str, start_epoch: int) -> float | None:
    """Decimal odds for a driver to win the upcoming F1 race on Kalshi
    (KXF1RACE series, 'Will X win ... Grand Prix?' markets), or None."""
    if not driver:
        return None
    async with _client() as client:
        try:
            r = await client.get(_KALSHI_MARKETS, params={
                "series_ticker": "KXF1RACE", "status": "open", "limit": 200})
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            return None
    for m in (data or {}).get("markets") or []:
        # Driver name: `yes_sub_title` ("Max Verstappen"); the title format
        # changed from "Will X win ...?" to "X to finish in first", so parse
        # both as a fallback.
        name = m.get("yes_sub_title")
        if not name:
            mt = _KALSHI_F1_Q.match(str(m.get("title") or ""))
            name = (mt.group(1) or mt.group(2)) if mt else None
        if not name or not _same_team(name, driver):
            continue
        ev_ts = _kalshi_event_ts(m)
        if ev_ts is not None and abs(ev_ts - start_epoch) > 72 * 3600:
            continue
        prob = _kalshi_yes_prob(m)
        return _to_decimal(prob) if prob else None
    return None


# Sports tracked as pairwise "A vs. B" events on Polymarket (like WNBA
# basketball): tag slugs + display label. UFC was removed (no Italian book
# prices it); MLB (baseball) is the only versus-style prediction sport now.
VERSUS_SPORTS = {
    "mlb": (["mlb"], "MLB"),
}


class PredictionMarketsClient:
    """Monitor-compatible client that tracks F1 and MLB on Polymarket
    (primary) and cross-checks on Kalshi. Probabilities convert to decimal
    odds so drops/steam behave like every other sport. Keyless and
    quota-free."""
    name = "prediction"

    def __init__(self):
        self.api_key = None
        self.use_mock = False
        self.requests_remaining = None
        self.quota_exhausted = False
        self.ip_blocked = False

    async def close(self):
        await _close_http()

    async def get_outright_matches(self, sources) -> list[dict]:
        """Tournament-winner / award outrights on Polymarket. Each qualifying
        event (one per competition, e.g. 'UEFA Champions League: 2027 Champion')
        becomes ONE match whose selections are the contenders - built from the
        event's binary 'Will <X> win ...?' sub-markets (Yes price = probability
        -> decimal odds = 1/Yes). Unlike game markets these are long-lived, so
        there is no start time / 60-minute window: they're tracked while the
        market is open (active & not closed).

        `sources`: list of {emoji, tag, title} dicts. `title` (lowercase
        substring of the event title, or None) pins the wanted event within the
        tag; only events above OUTRIGHT_MIN_LIQUIDITY are kept, and only
        contenders inside OUTRIGHT_PROB_BAND are tracked (the rest of the field
        is unpriced longshots)."""
        out: list[dict] = []
        seen: set = set()
        for src in sources:
            tag = src["tag"]
            title_sub = (src.get("title") or "").lower()
            emoji = src.get("emoji", "🏆")
            # Per-source liquidity floor (falls back to the global one) so a
            # noisy category - e.g. golf, where the tag pulls in thin minor
            # tours - can demand deeper markets than the default.
            floor = src.get("min_liquidity", OUTRIGHT_MIN_LIQUIDITY)
            try:
                events = await _gamma_events(tag)
            except Exception as e:
                logger.warning("outright fetch tag=%s failed: %s", tag, e)
                continue
            for e in events:
                eid = e.get("id")
                if eid in seen:
                    continue
                title = str(e.get("title") or "")
                if title_sub and title_sub not in title.lower():
                    continue
                try:
                    liquidity = float(e.get("liquidity") or 0)
                except (TypeError, ValueError):
                    liquidity = 0.0
                if liquidity < floor:
                    continue
                selections: list[dict] = []
                for m in e.get("markets") or []:
                    mt = _OUTRIGHT_WIN_Q.match(str(m.get("question") or ""))
                    if not mt:
                        continue
                    if _market_liquidity(m) < PM_MIN_LIQUIDITY:
                        continue  # 0-liquidity contender -> unreliable price
                    try:
                        outs = m.get("outcomes")
                        prices = m.get("outcomePrices")
                        if isinstance(outs, str):
                            outs = json.loads(outs)
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                    except Exception:
                        continue
                    if (not outs or not prices or len(outs) != len(prices)
                            or str(outs[0]).lower() != "yes"):
                        continue
                    try:
                        yes = float(prices[0])
                    except (TypeError, ValueError):
                        continue
                    if not (OUTRIGHT_PROB_BAND[0] <= yes <= OUTRIGHT_PROB_BAND[1]):
                        continue
                    price = _to_decimal(yes)
                    if not price:
                        continue
                    cand = mt.group(1).strip()
                    if _OUTRIGHT_PLACEHOLDER.match(cand):
                        continue
                    selections.append({
                        "market_key": "outright", "market_name": "Vincitore",
                        "outcome": cand, "point": None, "label": cand,
                        "price": price})
                if len(selections) >= 2:
                    seen.add(eid)
                    out.append({
                        "match_id": f"pm-outright-{eid}",
                        "tournament": title,
                        "player1": None, "player2": None,
                        "start_epoch": None,  # long-lived: no start / no window
                        "emoji": emoji,
                        "selections": selections})
        return out

    async def get_whale_condition_map(self, tags=WHALE_TAGS) -> dict:
        """conditionId -> (emoji, sport_label, market_title) for every market in
        the watched sports' tags, so a whale trade can be classified by its
        conditionId. Rebuilt on a slow cadence by the caller (markets change
        over hours, not seconds). Paginated (see _gamma_events): a truncated
        map silently misses whales on the markets that didn't fit."""
        condmap: dict[str, tuple] = {}
        for tag in tags:
            emoji, label = _WHALE_TAG_SPORT.get(tag, ("🐋", "Polymarket"))
            try:
                events = await _gamma_events(tag)
            except Exception as e:
                logger.warning("whale condmap tag=%s failed: %s", tag, e)
                continue
            for e in events:
                ev_title = str(e.get("title") or "")
                for m in e.get("markets") or []:
                    cid = m.get("conditionId")
                    if cid and cid not in condmap:
                        title = str(m.get("question") or ev_title)
                        condmap[cid] = (emoji, label, title)
        return condmap

    async def get_whale_trades(self, min_usd: float, limit: int = 100) -> list[dict]:
        """Recent platform-wide trades >= min_usd notional (Data API server-side
        CASH filter). Caller matches them to watched markets by conditionId.
        Raises on failure (never returns a silent []): the caller's first pass
        baselines the seen-set, and baselining on an empty failed read would
        replay up to `limit` old whales as new alerts on the next pass."""
        async with _client() as client:
            r = await client.get(_DATA_TRADES, params={
                "filterType": "CASH", "filterAmount": int(min_usd),
                "takerOnly": "true", "limit": limit})
            r.raise_for_status()
            data = r.json()
        out: list[dict] = []
        for t in data if isinstance(data, list) else []:
            try:
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            out.append({
                "tx": t.get("transactionHash"),
                "cond": t.get("conditionId"),
                "side": str(t.get("side") or "").upper(),
                "outcome": t.get("outcome"),
                "price": price,
                "size": size,
                "usd": size * price,
                "title": str(t.get("title") or ""),
                "name": t.get("name") or t.get("pseudonym") or "",
                "wallet": t.get("proxyWallet") or "",
                "ts": t.get("timestamp"),
            })
        return out

    async def get_pinnacle_matches(self, sport: str, start_epoch: int,
                                   end_epoch: int, tournament_filter=None):
        if sport in VERSUS_SPORTS:
            return await self._get_versus_matches(sport, start_epoch, end_epoch)
        if sport != "f1":
            return []
        try:
            events = await _gamma_events("f1")
        except Exception:
            return []
        out: list[dict] = []
        for e in events:
            title = str(e.get("title") or "")
            gp = title.split(":")[0].strip()
            suffix = title.split(":", 1)[1].strip() if ":" in title else ""
            spec = next((t for t in F1_EVENT_MARKETS
                         if t[0].lower() == suffix.lower()), None)
            if spec is None or not gp:
                continue
            _, mkey, mlabel = spec
            selections: list[dict] = []
            start_ts: int | None = None
            pair_cache: dict[str, dict] = {}
            for m in e.get("markets") or []:
                game_ts = (_parse_ts(m.get("gameStartTime"))
                           or _parse_ts(e.get("endDate")))
                if game_ts is None or not (start_epoch < game_ts <= end_epoch):
                    continue
                try:
                    outcomes = m.get("outcomes")
                    prices = m.get("outcomePrices")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                except Exception:
                    continue
                if not outcomes or not prices or len(outcomes) != len(prices):
                    continue
                if _market_liquidity(m) < PM_MIN_LIQUIDITY:
                    continue  # no order book -> price untradeable/unreliable
                start_ts = game_ts if start_ts is None else min(start_ts, game_ts)
                q = str(m.get("question") or "")
                if mkey == "h2h":
                    hq = _F1_H2H_Q.match(q)
                    if not hq:
                        continue
                    a, b = hq.group(1).strip(), hq.group(2).strip()
                    pid = f"pm-{e.get('id')}-{m.get('id')}"
                    pair = pair_cache.setdefault(pid, {
                        "match_id": pid, "tournament": f"F1 · {gp}",
                        "player1": a, "player2": b, "start_epoch": game_ts,
                        "selections": []})
                    for name, p in zip(outcomes, prices):
                        price = _to_decimal(p)
                        if price and F1_PROB_BAND[0] <= float(p) <= F1_PROB_BAND[1]:
                            pair["selections"].append({
                                "market_key": "h2h", "market_name": "Head-to-Head",
                                "outcome": name, "point": None, "label": name,
                                "price": price})
                    continue
                yq = _F1_YES_Q.match(q)
                if not yq or not outcomes or outcomes[0].lower() != "yes":
                    continue
                driver = yq.group(1).strip()
                p = float(prices[0]) if prices else 0
                price = _to_decimal(p)
                if price and F1_PROB_BAND[0] <= p <= F1_PROB_BAND[1]:
                    selections.append({
                        "market_key": mkey, "market_name": mlabel,
                        "outcome": driver, "point": None, "label": driver,
                        "price": price})
            for pair in pair_cache.values():
                if len(pair["selections"]) == 2:
                    out.append(pair)
            if selections and start_ts:
                out.append({
                    "match_id": f"pm-{e.get('id')}-{mkey}",
                    "tournament": f"F1 · {gp}",
                    "player1": gp, "player2": mlabel,
                    "start_epoch": start_ts,
                    "selections": selections,
                })
        return out

    async def _get_versus_matches(self, sport: str, start_epoch: int,
                                  end_epoch: int) -> list[dict]:
        """Pairwise 'A vs. B' events (MLB games) as matches with a two-way h2h
        market each. Same normalization/matching rules as the alert-time
        lookups."""
        tags, label = VERSUS_SPORTS[sport]
        out: list[dict] = []
        for tag in tags:
            try:
                # Server-side filter on the event's game start: only the games
                # starting inside the window (a page or two) instead of the
                # tag's ~330 open events (season futures, props, ...).
                events = await _gamma_events(
                    tag, start_time_min=_iso(start_epoch),
                    start_time_max=_iso(end_epoch))
            except Exception:
                continue
            for e in events:
                # Title is a prefilter only - event titles carry prefixes
                # ("Dana White's Contender Series: A vs B (Weight)") so the
                # fighter/team names come from the market outcomes instead.
                if not re.search(r"\s+vs\.?\s+", str(e.get("title") or ""), re.I):
                    continue
                for m in e.get("markets") or []:
                    # Game events also carry spread ("Spread: X (-1.5)", team-name
                    # outcomes) and totals (Over/Under) markets: only the
                    # moneyline is the match-winner price. When the moneyline
                    # was filtered out (thin book) the loop used to fall through
                    # to one of those and track it as the moneyline.
                    smt = m.get("sportsMarketType")
                    if smt and smt != "moneyline":
                        continue
                    game_ts = (_parse_ts(m.get("gameStartTime"))
                               or _parse_ts(e.get("endDate")))
                    if game_ts is None or not (start_epoch < game_ts <= end_epoch):
                        continue
                    try:
                        outcomes = m.get("outcomes")
                        prices = m.get("outcomePrices")
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                    except Exception:
                        continue
                    if (not outcomes or not prices
                            or len(outcomes) != 2 or len(prices) != 2
                            or outcomes[0].lower() in ("yes", "no", "over", "under")
                            or outcomes[1].lower() in ("yes", "no", "over", "under")):
                        continue
                    if _market_liquidity(m) < PM_MIN_LIQUIDITY:
                        continue  # no order book -> price untradeable/unreliable
                    selections = []
                    ok = True
                    for i, (name, p) in enumerate(zip(outcomes, prices)):
                        price = _to_decimal(p)
                        if not price or not (0.02 <= float(p) <= 0.97):
                            ok = False
                            break
                        selections.append({
                            "market_key": "h2h", "market_name": "Moneyline",
                            "outcome": "home" if i == 0 else "away",
                            "point": None,
                            "label": name, "price": price})
                    if not ok or len(selections) != 2:
                        continue
                    out.append({
                        "match_id": f"pm-{e.get('id')}-{m.get('id')}",
                        "tournament": label,
                        "player1": outcomes[0], "player2": outcomes[1],
                        "start_epoch": game_ts,
                        "selections": selections,
                    })
                    break  # first usable market per event
        return out
