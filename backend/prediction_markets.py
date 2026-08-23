"""Prediction-market cross-checks (Polymarket, Kalshi) for drop alerts.

Both platforms expose keyless, quota-free read-only APIs - unlike the odds
providers, these cost nothing per lookup:
- Polymarket Gamma: game events titled "TeamA vs. TeamB"; the market carries
  `outcomes` (["TeamA","TeamB"]) and `outcomePrices` (probabilities 0-1).
- Kalshi: per-game binary markets grouped by ticker prefix
  (KXWNBAGAME-26AUG25PDXDAL-PDX / -DAL), titles "<Team> wins",
  yes_bid/yes_ask in cents.

Prices are probabilities: decimal odds = 1 / probability. Liquidity is thin
far from tip-off and quotes can be absent (yes_ask None), so every lookup
returns None liberally - a missing cross-check must never break an alert.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
_KALSHI_MARKETS = "https://api.elections.kalshi.com/trade-api/v2/markets"

# sport -> Polymarket tag slugs carrying game moneylines.
POLYMARKET_TAGS = {
    "basketball": ["nba", "wnba"],
    "football": ["epl", "ucl"],
    # Per-match tennis moneylines; outcomes are last names, matched via
    # word overlap; stale/resolved events are rejected by the time gate
    # and the 2%-97% probability band.
    "tennis": ["tennis"],
}
# sport -> Kalshi game-winner series tickers. Basketball only for now:
# football titles are city-based and error-prone to match.
KALSHI_SERIES = {
    "basketball": ["KXNBAGAME", "KXWNBAGAME"],
}

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
    async with httpx.AsyncClient(timeout=15) as client:
        for tag in tags:
            try:
                r = await client.get(_GAMMA_EVENTS, params={
                    "limit": 500, "active": "true", "closed": "false",
                    "tag_slug": tag,
                })
                events = r.json() if r.status_code == 200 else []
            except Exception:
                continue
            for e in events if isinstance(events, list) else []:
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


async def kalshi_price(sport: str, home: str, away: str,
                       start_epoch: int, side: str) -> float | None:
    """Decimal odds for `side` to win the game, or None."""
    series_list = KALSHI_SERIES.get(sport)
    if not series_list or not side:
        return None
    shared = _shared_words(home, away)
    async with httpx.AsyncClient(timeout=15) as client:
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
                    try:
                        close = datetime.fromisoformat(
                            str(m.get("close_time") or "").replace("Z", "+00:00"))
                        if abs((close.timestamp() - start_epoch)) <= 36 * 3600:
                            ok_time = True
                    except ValueError:
                        pass
                if len(sides) != 2 or not ok_time:
                    continue
                names = list(sides)
                if not ((_same_team(names[0], home, shared) and _same_team(names[1], away, shared))
                        or (_same_team(names[0], away, shared) and _same_team(names[1], home, shared))):
                    continue
                for name, m in sides.items():
                    if _same_team(name, side, shared):
                        cents = m.get("yes_ask") or m.get("yes_bid")
                        price = _to_decimal((cents or 0) / 100) if cents else None
                        if price:
                            logger.info("kalshi %s: %s @ %s", series, side, price)
                        return price
    return None


async def kalshi_f1_price(driver: str, start_epoch: int) -> float | None:
    """Decimal odds for a driver to win the upcoming F1 race on Kalshi
    (KXF1RACE series, 'Will X win ... Grand Prix?' markets), or None."""
    if not driver:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(_KALSHI_MARKETS, params={
                "series_ticker": "KXF1RACE", "status": "open", "limit": 200})
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            return None
    win_q = re.compile(r"^Will\s+(.+?)\s+win\b", re.I)
    for m in (data or {}).get("markets") or []:
        mt = win_q.match(str(m.get("title") or ""))
        if not mt or not _same_team(mt.group(1), driver):
            continue
        close_ts = _parse_ts(m.get("close_time"))
        if close_ts is not None and abs(close_ts - start_epoch) > 72 * 3600:
            continue
        cents = m.get("yes_ask") or m.get("yes_bid")
        return _to_decimal((cents or 0) / 100) if cents else None
    return None


# Sports tracked as pairwise "A vs. B" events on Polymarket (like WNBA
# basketball): tag slugs + display label.
VERSUS_SPORTS = {
    "mlb": (["mlb"], "MLB"),
    "ufc": (["ufc"], "UFC"),
}


class PredictionMarketsClient:
    """Monitor-compatible client that tracks F1, MLB and UFC on Polymarket
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
        pass

    async def get_pinnacle_matches(self, sport: str, start_epoch: int,
                                   end_epoch: int, tournament_filter=None):
        if sport in VERSUS_SPORTS:
            return await self._get_versus_matches(sport, start_epoch, end_epoch)
        if sport != "f1":
            return []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(_GAMMA_EVENTS, params={
                    "limit": 500, "active": "true", "closed": "false",
                    "tag_slug": "f1"})
                events = r.json() if r.status_code == 200 else []
        except Exception:
            return []
        out: list[dict] = []
        for e in events if isinstance(events, list) else []:
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
        """Pairwise 'A vs. B' events (MLB games, UFC fights) as matches with
        a two-way h2h market each. Same normalization/matching rules as the
        alert-time lookups."""
        tags, label = VERSUS_SPORTS[sport]
        out: list[dict] = []
        for tag in tags:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.get(_GAMMA_EVENTS, params={
                        "limit": 500, "active": "true", "closed": "false",
                        "tag_slug": tag})
                    events = r.json() if r.status_code == 200 else []
            except Exception:
                continue
            for e in events if isinstance(events, list) else []:
                # Title is a prefilter only - event titles carry prefixes
                # ("Dana White's Contender Series: A vs B (Weight)") so the
                # fighter/team names come from the market outcomes instead.
                if not re.search(r"\s+vs\.?\s+", str(e.get("title") or ""), re.I):
                    continue
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
                    if (not outcomes or not prices
                            or len(outcomes) != 2 or len(prices) != 2
                            or outcomes[0].lower() in ("yes", "no")
                            or outcomes[1].lower() in ("yes", "no")):
                        continue
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
