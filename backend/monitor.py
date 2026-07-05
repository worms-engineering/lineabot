"""Tennis value-bet monitor - core scan logic (OddsPapi v4).

Compares Pinnacle (sharp) Over/Under total-games markets against soft books
(Bet365, Betfair, Snai) fetched via the OddsPapi v4 public API. Emits Telegram
alerts whenever soft odds show positive EV vs the Pinnacle no-vig fair line by
more than the configured edge threshold.

v4 specifics
------------
* Odds are nested `bookmakerOdds -> {book} -> markets -> {marketId} ->
  outcomes -> {outcomeId} -> players["0"].price`.
* Pinnacle and the soft books SHARE the same numeric marketId / outcomeId, so
  the value comparison joins on those ids directly.
* Only Pinnacle exposes the human-readable line inside `bookmakerOutcomeId`
  (e.g. "38.5/over"); soft books carry an opaque internal id there. The line is
  therefore always taken from Pinnacle.
* The scan discovers matches with a single /fixtures?from&to call (the fresh
  schedule, with player and tournament names embedded), then fetches odds only
  for that handful of tournaments and joins them back by fixtureId. This avoids
  sweeping every active tournament and drops a scan from ~40 calls to a handful.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from oddspapi_client import OddsPapiClient
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

SHARP_BOOK = "pinnacle"
SOFT_BOOKS_DEFAULT = ["bet365", "betfair", "snai", "eurobet", "goldbet"]

# Map the canonical book names used in settings / the UI to the v4 API slugs.
# Betfair is the exchange feed; Snai is a clone of Sisal, so the v4 response is
# keyed "sisal.it" (the odds are identical). We always read whichever single
# book the response carries rather than relying on this key.
BOOK_SLUGS = {
    "pinnacle": "pinnacle",
    "bet365": "bet365",
    "betfair": "betfair-ex",
    "snai": "snai.it",
    "eurobet": "eurobet.it",
    "goldbet": "goldbet.it",
}

# Bookmaker display labels
BOOK_LABELS = {
    "pinnacle": "Pinnacle",
    "bet365": "Bet365",
    "betfair": "Betfair",
    "snai": "Snai",
    "eurobet": "Eurobet",
    "goldbet": "Goldbet",
}

# Look-ahead window: only alert on matches starting within the next hour.
WINDOW_SECONDS = 60 * 60

# A full-match total-games line is always well above per-set game totals
# (~8-13) and total-sets lines (~2.5-4.5). This threshold cleanly isolates the
# full-match "Total Games" market from those other over/under markets.
MIN_GAMES_LINE = 15.0

# Pinnacle encodes total-games outcomes as "<line>/over" or "<line>/under".
# Per-player (teamTotal) outcomes carry a "home/"/"away/" prefix and are
# intentionally excluded by anchoring the regex at the start of the string.
_TOTALS_OUTCOME_RE = re.compile(r"^(\d+(?:\.\d+)?)/(over|under)$")

MARKET_NAME = "Total Games O/U"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _no_vig_two_way(price_a: float, price_b: float) -> tuple[float, float]:
    """Return no-vig fair probabilities for a two-way market."""
    ia, ib = 1.0 / price_a, 1.0 / price_b
    total = ia + ib
    return ia / total, ib / total


def _parse_start_epoch(value: Any) -> int | None:
    """Convert an ISO-8601 startTime (e.g. '2026-07-04T15:40:00.000Z') to epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _outcome_price(outcome: dict) -> tuple[float | None, str | None, bool]:
    """Extract (price, bookmakerOutcomeId, active) from an outcome's player 0."""
    player = (outcome.get("players") or {}).get("0") or {}
    price = player.get("price")
    price = float(price) if price is not None else None
    return price, player.get("bookmakerOutcomeId"), player.get("active") is not False


def _pinnacle_total_games(book_odds: dict) -> dict[str, dict]:
    """Parse Pinnacle's full-match total-games markets.

    Returns { marketId: {line, over_id, under_id, over_price, under_price} }
    keeping only markets where both Over and Under are currently active.
    """
    result: dict[str, dict] = {}
    for market_id, market in (book_odds.get("markets") or {}).items():
        if market.get("marketActive") is False:
            continue
        sides: dict[str, dict] = {}
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            price, boid, active = _outcome_price(outcome)
            if price is None or not active or not boid:
                continue
            m = _TOTALS_OUTCOME_RE.match(boid)
            if not m:
                continue
            line = float(m.group(1))
            if line < MIN_GAMES_LINE:
                continue
            sides[m.group(2)] = {"outcome_id": outcome_id, "price": price, "line": line}
        if "over" in sides and "under" in sides:
            result[market_id] = {
                "line": sides["over"]["line"],
                "over_id": sides["over"]["outcome_id"],
                "under_id": sides["under"]["outcome_id"],
                "over_price": sides["over"]["price"],
                "under_price": sides["under"]["price"],
            }
    return result


def _single_book_odds(fixture: dict) -> dict | None:
    """Return the one bookmaker's odds object from a fixture.

    Each /odds-by-tournaments call is for a single bookmaker, so `bookmakerOdds`
    holds at most one entry. Its key can differ from the requested slug (e.g.
    Snai -> "sisal.it"), so we take the value regardless of the key.
    """
    book_odds = fixture.get("bookmakerOdds") or {}
    return next(iter(book_odds.values()), None)


def _book_prices(book_odds: dict) -> dict[str, dict[str, float]]:
    """Parse any book's active prices into { marketId: { outcomeId: price } }."""
    result: dict[str, dict[str, float]] = {}
    if not book_odds or book_odds.get("suspended"):
        return result
    for market_id, market in (book_odds.get("markets") or {}).items():
        if market.get("marketActive") is False:
            continue
        prices: dict[str, float] = {}
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            price, _boid, active = _outcome_price(outcome)
            if price is None or not active:
                continue
            prices[outcome_id] = price
        if prices:
            result[market_id] = prices
    return result


class TennisMonitor:
    def __init__(self, db, edge_threshold: float = 0.03):
        self.db = db
        self.oddspapi = OddsPapiClient()
        self.telegram = TelegramClient()
        self.edge_threshold = edge_threshold
        self.soft_books = list(SOFT_BOOKS_DEFAULT)
        self._lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}

    async def close(self):
        await self.oddspapi.close()

    async def load_settings(self):
        cfg = await self.db.settings.find_one({"_id": "config"})
        if cfg:
            self.edge_threshold = float(cfg.get("edge_threshold", self.edge_threshold))
            self.soft_books = list(cfg.get("soft_books") or SOFT_BOOKS_DEFAULT)
            token = cfg.get("telegram_token")
            chat_id = cfg.get("telegram_chat_id")
            if token:
                self.telegram.token = token
            if chat_id:
                self.telegram.chat_id = chat_id

    async def save_settings(self, edge_threshold: float | None = None,
                            soft_books: list[str] | None = None,
                            telegram_token: str | None = None,
                            telegram_chat_id: str | None = None):
        update: dict[str, Any] = {}
        if edge_threshold is not None:
            self.edge_threshold = float(edge_threshold)
            update["edge_threshold"] = self.edge_threshold
        if soft_books is not None:
            self.soft_books = list(soft_books)
            update["soft_books"] = self.soft_books
        if telegram_token is not None:
            self.telegram.token = telegram_token
            update["telegram_token"] = telegram_token
        if telegram_chat_id is not None:
            self.telegram.chat_id = telegram_chat_id
            update["telegram_chat_id"] = telegram_chat_id
        if update:
            await self.db.settings.update_one(
                {"_id": "config"}, {"$set": update}, upsert=True
            )

    async def scan_once(self, dry_run_notify: bool = False) -> dict:
        async with self._lock:
            started = _now()
            try:
                result = await self._scan_impl(dry_run_notify=dry_run_notify)
                self.last_scan_at = started
                self.last_scan_error = None
                self.last_scan_stats = result
                await self.db.scans.insert_one({
                    "_id": str(uuid.uuid4()),
                    "started_at": started.isoformat(),
                    "stats": result,
                    "error": None,
                })
                return result
            except Exception as e:
                logger.exception("scan failed")
                self.last_scan_at = started
                self.last_scan_error = str(e)
                await self.db.scans.insert_one({
                    "_id": str(uuid.uuid4()),
                    "started_at": started.isoformat(),
                    "stats": {},
                    "error": str(e),
                })
                return {"error": str(e)}

    async def _scan_impl(self, dry_run_notify: bool) -> dict:
        now_ts = int(_now().timestamp())
        end_ts = now_ts + WINDOW_SECONDS

        # 1) One /fixtures call: the fresh schedule of matches starting in the
        #    window, already carrying player & tournament names. This is the only
        #    tournament-discovery step, so we fetch odds for just this handful of
        #    tournaments instead of sweeping every active one.
        schedule = await self.oddspapi.get_fixtures(now_ts, end_ts)
        window_meta: list[dict] = []
        for fx in schedule:
            if not fx.get("hasOdds"):
                continue
            start_epoch = _parse_start_epoch(fx.get("startTime"))
            if start_epoch is None or not (now_ts < start_epoch <= end_ts):
                continue
            fx["_startEpoch"] = start_epoch
            window_meta.append(fx)

        fixture_meta_by_id = {fx["fixtureId"]: fx for fx in window_meta}
        relevant_ids = sorted({fx["tournamentId"] for fx in window_meta})

        # 2) Odds for only those tournaments (one sequential call per book to
        #    respect the ~1 req/s rate limit), joined back to the schedule by
        #    fixtureId so stale phantom fixtures are dropped.
        odds_by_book: dict[str, dict[str, dict]] = {}
        if relevant_ids:
            for book in [SHARP_BOOK] + list(self.soft_books):
                slug = BOOK_SLUGS.get(book, book)
                fixtures = await self.oddspapi.get_odds_by_tournaments(slug, relevant_ids)
                odds_by_book[book] = {
                    f["fixtureId"]: f
                    for f in fixtures
                    if f["fixtureId"] in fixture_meta_by_id
                }

        pin_by_id = odds_by_book.get(SHARP_BOOK, {})

        matches_payload: list[dict] = []
        value_bets_all: list[dict] = []
        alerts_sent = 0

        for fx_meta in window_meta:
            fixture_id = fx_meta["fixtureId"]
            pin_fx = pin_by_id.get(fixture_id)
            pin_book = _single_book_odds(pin_fx) if pin_fx else None
            if not pin_book or pin_book.get("suspended"):
                continue

            pin_markets = _pinnacle_total_games(pin_book)
            match_value_bets: list[dict] = []

            # Parse each soft book's prices once for this fixture.
            soft_prices_by_book: dict[str, dict[str, dict[str, float]]] = {}
            for soft in self.soft_books:
                soft_fx = odds_by_book.get(soft, {}).get(fixture_id)
                if soft_fx:
                    soft_prices_by_book[soft] = _book_prices(_single_book_odds(soft_fx) or {})

            for market_id, info in pin_markets.items():
                p_over, p_under = _no_vig_two_way(info["over_price"], info["under_price"])

                for soft, soft_prices in soft_prices_by_book.items():
                    market_prices = soft_prices.get(market_id)
                    if not market_prices:
                        continue

                    for side_name, outcome_id, pin_price, fair_p in (
                        ("Over", info["over_id"], info["over_price"], p_over),
                        ("Under", info["under_id"], info["under_price"], p_under),
                    ):
                        soft_price = market_prices.get(outcome_id)
                        if not soft_price:
                            continue
                        ev = soft_price * fair_p - 1.0
                        match_value_bets.append({
                            "fixture_id": fixture_id,
                            "market_id": market_id,
                            "market_name": MARKET_NAME,
                            "handicap": info["line"],
                            "side": side_name,
                            "soft_book": soft,
                            "soft_price": round(soft_price, 3),
                            "pinnacle_price": round(pin_price, 3),
                            "fair_price": round(1.0 / fair_p, 3) if fair_p > 0 else None,
                            "fair_prob": round(fair_p, 4),
                            "edge": round(ev, 4),
                            "is_value": ev >= self.edge_threshold,
                        })

            match_info = {
                "fixture_id": fixture_id,
                "start_time": fx_meta.get("_startEpoch"),
                "true_start_time": _parse_start_epoch(fx_meta.get("trueStartTime")),
                "tournament": fx_meta.get("tournamentName"),
                "category": fx_meta.get("categoryName"),
                "player1": fx_meta.get("participant1Name")
                or fx_meta.get("participant1Id"),
                "player2": fx_meta.get("participant2Name")
                or fx_meta.get("participant2Id"),
                "value_bets": match_value_bets,
            }
            matches_payload.append(match_info)
            value_bets_all.extend(match_value_bets)

            # Send alerts for value bets not already alerted recently.
            for vb in match_value_bets:
                if not vb["is_value"]:
                    continue
                dedup_key = f"{vb['fixture_id']}:{vb['market_id']}:{vb['side']}:{vb['soft_book']}"
                exists = await self.db.alerts.find_one({"dedup_key": dedup_key})
                if exists:
                    continue
                text = self._format_alert(match_info, vb)
                tg_result = {"ok": False}
                if not dry_run_notify:
                    try:
                        tg_result = await self.telegram.send_message(text)
                    except Exception as e:
                        tg_result = {"ok": False, "error": str(e)}
                alert_doc = {
                    "_id": str(uuid.uuid4()),
                    "dedup_key": dedup_key,
                    "created_at": _now().isoformat(),
                    "fixture_id": vb["fixture_id"],
                    "player1": match_info["player1"],
                    "player2": match_info["player2"],
                    "tournament": match_info["tournament"],
                    "market_name": vb["market_name"],
                    "handicap": vb["handicap"],
                    "side": vb["side"],
                    "soft_book": vb["soft_book"],
                    "soft_price": vb["soft_price"],
                    "pinnacle_price": vb["pinnacle_price"],
                    "fair_price": vb["fair_price"],
                    "edge": vb["edge"],
                    "telegram_ok": bool(tg_result.get("ok")),
                    "telegram_response": tg_result,
                    "message": text,
                }
                await self.db.alerts.insert_one(alert_doc)
                alerts_sent += 1

        # Save the latest scan snapshot for the UI.
        await self.db.snapshots.update_one(
            {"_id": "latest"},
            {"$set": {
                "updated_at": _now().isoformat(),
                "edge_threshold": self.edge_threshold,
                "soft_books": self.soft_books,
                "matches": matches_payload,
            }},
            upsert=True,
        )

        stats = {
            "fixtures_scanned": len(window_meta),
            "matches_with_odds": sum(1 for m in matches_payload if m["value_bets"]),
            "value_bets_found": sum(1 for vb in value_bets_all if vb["is_value"]),
            "alerts_sent": alerts_sent,
        }
        return stats

    def _format_alert(self, match: dict, vb: dict) -> str:
        pct = vb["edge"] * 100
        soft_label = BOOK_LABELS.get(vb["soft_book"], vb["soft_book"].title())
        line = vb["handicap"]
        line_str = f" {line}" if line is not None else ""
        start_ts = match.get("start_time")
        start_str = ""
        if start_ts:
            start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            start_str = start_dt.strftime("%H:%M UTC")
        return (
            f"<b>VALUE BET — Tennis</b>\n"
            f"{match['player1']} vs {match['player2']}\n"
            f"{match.get('tournament') or ''} · start {start_str}\n"
            f"<b>{vb['side']}{line_str}</b> @ <b>{vb['soft_price']:.2f}</b> ({soft_label})\n"
            f"Pinnacle: {vb['pinnacle_price']:.2f} · Fair: {vb['fair_price']:.2f}\n"
            f"Edge: <b>+{pct:.2f}%</b>"
        )
