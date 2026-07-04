"""Tennis value-bet monitor - core scan logic.

Compares Pinnacle (sharp) Over/Under Games markets against soft books
(Bet365, Betfair, Snai) fetched via OddsPapi v5. Emits Telegram alerts
whenever soft odds show positive EV vs Pinnacle no-vig fair line by more
than the configured edge threshold.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from oddspapi_client import OddsPapiClient
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

SHARP_BOOK = "pinnacle"
SOFT_BOOKS_DEFAULT = ["bet365", "betfair", "snai"]

# Bookmaker display labels
BOOK_LABELS = {
    "pinnacle": "Pinnacle",
    "bet365": "Bet365",
    "betfair": "Betfair",
    "snai": "Snai",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _no_vig_two_way(price_a: float, price_b: float) -> tuple[float, float]:
    """Return no-vig fair probabilities for a two-way market."""
    ia, ib = 1.0 / price_a, 1.0 / price_b
    total = ia + ib
    return ia / total, ib / total


def _group_totals_by_market(odds_map: dict[str, dict]) -> dict[int, dict[str, dict]]:
    """From an odds map for one bookmaker, keep only Over/Under totals grouped by marketId.

    Returns { marketId: { 'over': quote, 'under': quote } } but we don't know Over/Under
    from odds alone (marketId identifies the market, outcomeId identifies Over vs Under).
    We keep both outcomes and let the caller infer Over/Under via the /markets metadata.
    """
    result: dict[int, dict[int, dict]] = {}
    for quote in odds_map.values():
        if not quote.get("active"):
            continue
        if quote.get("marketActive") is False:
            continue
        market_id = quote.get("marketId")
        outcome_id = quote.get("outcomeId")
        if market_id is None or outcome_id is None:
            continue
        result.setdefault(market_id, {})[outcome_id] = quote
    return result


class TennisMonitor:
    def __init__(self, db, edge_threshold: float = 0.03):
        self.db = db
        self.oddspapi = OddsPapiClient()
        self.telegram = TelegramClient()
        self.edge_threshold = edge_threshold
        self.soft_books = list(SOFT_BOOKS_DEFAULT)
        self._lock = asyncio.Lock()
        self._markets_cache: dict[int, dict] | None = None
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

    async def _load_markets(self) -> dict[int, dict]:
        if self._markets_cache is not None:
            return self._markets_cache
        markets = await self.oddspapi.get_markets_for_sport(12)
        self._markets_cache = {m["marketId"]: m for m in markets}
        return self._markets_cache

    def _is_total_games_market(self, market: dict) -> bool:
        """Match markets that are Over/Under total games for the full match."""
        if not market:
            return False
        if market.get("marketType") != "totals":
            return False
        if market.get("playerProp"):
            return False
        period = (market.get("period") or "").lower()
        # Full-match games total (exclude per-set totals)
        if period not in {"fulltime", "match", "result", "regulartime"}:
            return False
        name = (market.get("marketName") or "").lower()
        short = (market.get("marketNameShort") or "").lower()
        # Tennis totals almost always refer to games; keep filter permissive
        if "set" in name and "sets" not in name:
            return False
        if "games" in name or "games" in short or market.get("marketType") == "totals":
            return True
        return False

    @staticmethod
    def _outcome_label(market: dict, outcome_id: int) -> str | None:
        for o in market.get("outcomes") or []:
            if o.get("outcomeId") == outcome_id:
                return (o.get("outcomeName") or "").lower()
        return None

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
        end_ts = now_ts + 60 * 60  # next 60 minutes

        books = [SHARP_BOOK] + self.soft_books
        fixtures = await self.oddspapi.get_tennis_fixtures_upcoming(
            start_from_epoch=now_ts,
            start_to_epoch=end_ts,
            bookmakers=books,
        )

        # Exclude anything that is already live or has already started
        fixtures = [
            f for f in fixtures
            if not (f.get("status") or {}).get("live")
            and (f.get("status") or {}).get("statusId") == 0
            and f.get("startTime", 0) > now_ts
        ]

        markets_meta = await self._load_markets()

        matches_payload: list[dict] = []
        value_bets_all: list[dict] = []
        alerts_sent = 0

        for fx in fixtures:
            fixture_id = fx["fixtureId"]
            try:
                odds_resp = await self.oddspapi.get_fixture_odds(fixture_id, books)
            except Exception as e:
                logger.warning("odds fetch failed for %s: %s", fixture_id, e)
                continue

            odds_by_book = odds_resp.get("odds") or {}
            pin_odds = odds_by_book.get(SHARP_BOOK)
            if not pin_odds:
                continue

            pin_by_market = _group_totals_by_market(pin_odds)
            match_value_bets: list[dict] = []

            for market_id, outcomes in pin_by_market.items():
                if len(outcomes) < 2:
                    continue
                mkt_meta = markets_meta.get(market_id)
                if not self._is_total_games_market(mkt_meta or {}):
                    continue
                handicap = (mkt_meta or {}).get("handicap")

                # Identify Over and Under outcomes for this market
                outcome_ids = sorted(outcomes.keys())
                # Prefer using market metadata to distinguish Over vs Under
                over_id = under_id = None
                for oid in outcome_ids:
                    label = self._outcome_label(mkt_meta or {}, oid) or ""
                    if "over" in label or label == "o":
                        over_id = oid
                    elif "under" in label or label == "u":
                        under_id = oid
                if over_id is None or under_id is None:
                    # Fallback: smaller outcomeId is Over by OddsPapi convention
                    over_id, under_id = outcome_ids[0], outcome_ids[1]

                pin_over = outcomes[over_id]["price"]
                pin_under = outcomes[under_id]["price"]
                p_over, p_under = _no_vig_two_way(pin_over, pin_under)

                # For each soft book, look up the SAME market_id and same outcome IDs
                for soft in self.soft_books:
                    soft_odds = odds_by_book.get(soft)
                    if not soft_odds:
                        continue
                    soft_by_market = _group_totals_by_market(soft_odds)
                    soft_outcomes = soft_by_market.get(market_id)
                    if not soft_outcomes:
                        continue
                    soft_over_q = soft_outcomes.get(over_id)
                    soft_under_q = soft_outcomes.get(under_id)
                    for side_name, soft_q, fair_p in (
                        ("Over", soft_over_q, p_over),
                        ("Under", soft_under_q, p_under),
                    ):
                        if not soft_q:
                            continue
                        soft_price = soft_q["price"]
                        ev = soft_price * fair_p - 1.0
                        row = {
                            "fixture_id": fixture_id,
                            "market_id": market_id,
                            "market_name": (mkt_meta or {}).get("marketName") or "Totals",
                            "handicap": handicap,
                            "side": side_name,
                            "soft_book": soft,
                            "soft_price": round(soft_price, 3),
                            "pinnacle_price": round(
                                pin_over if side_name == "Over" else pin_under, 3
                            ),
                            "fair_price": round(1.0 / fair_p, 3) if fair_p > 0 else None,
                            "fair_prob": round(fair_p, 4),
                            "edge": round(ev, 4),
                            "is_value": ev >= self.edge_threshold,
                        }
                        match_value_bets.append(row)

            # Push match info regardless (empty list means no value found)
            participants = fx.get("participants") or {}
            match_info = {
                "fixture_id": fixture_id,
                "start_time": fx.get("startTime"),
                "true_start_time": fx.get("trueStartTime"),
                "tournament": (fx.get("tournament") or {}).get("tournamentName"),
                "category": (fx.get("tournament") or {}).get("categoryName"),
                "player1": participants.get("participant1Name") or participants.get("participant1ShortName"),
                "player2": participants.get("participant2Name") or participants.get("participant2ShortName"),
                "value_bets": match_value_bets,
            }
            matches_payload.append(match_info)
            value_bets_all.extend(match_value_bets)

            # Send alerts for value bets not already alerted recently
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

        # Save the latest scan snapshot for the UI
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
            "fixtures_scanned": len(fixtures),
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
