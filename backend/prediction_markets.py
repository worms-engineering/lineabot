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
}
# sport -> Kalshi game-winner series tickers. Basketball only for now:
# football titles are city-based and error-prone to match.
KALSHI_SERIES = {
    "basketball": ["KXNBAGAME", "KXWNBAGAME"],
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
