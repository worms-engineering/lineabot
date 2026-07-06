"""Tennis Pinnacle line-drop monitor - core scan logic (OddsPapi v4).

Tracks Pinnacle (sharp) prices on the Match Winner (H2H) and the main Total
Games line for matches starting soon, and fires a Telegram alert when a price
drops by more than the configured threshold between two scans (a "steam" move).

Rationale: when Pinnacle drops, sharp money has moved. Slow soft books (especially
the Italian ones) still show the old, higher price for a while, so there is time
to act. Only Pinnacle is queried, so a scan costs very few API calls.

Tracking can be turned on/off at runtime (dashboard toggle); while off, scans are
skipped and no OddsPapi calls are spent.

v4 specifics: odds are nested `bookmakerOdds -> {book} -> markets -> {marketId} ->
outcomes -> {outcomeId} -> players["0"]`. Pinnacle exposes the H2H as the moneyline
whose bookmakerMarketId ends "/0/moneyline" (home=player1 / away=player2), and the
total-games line inside bookmakerOutcomeId (e.g. "37.0/over") with the main line
flagged players.0.mainLine == true.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from oddspapi_client import OddsPapiClient
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

SHARP_BOOK = "pinnacle"

# Only track matches starting within the next hour.
WINDOW_SECONDS = 60 * 60

# Full-match total-games lines sit well above per-set/total-sets lines.
MIN_GAMES_LINE = 15.0
_TOTALS_OUTCOME_RE = re.compile(r"^(\d+(?:\.\d+)?)/(over|under)$")

DEFAULT_DROP_THRESHOLD = 0.05  # alert when a price drops >= 5% between scans

# Ignore a previous observation older than this when computing a drop (e.g. after
# tracking was paused): just re-baseline instead of firing a false alert.
MAX_BASELINE_AGE_SECONDS = 30 * 60
# Drop tracked-line state not refreshed in this long (match already started).
LINE_STATE_TTL_SECONDS = 6 * 60 * 60

H2H_MARKET_NAME = "Match Winner"
TOTALS_MARKET_NAME = "Total Games"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_start_epoch(value: Any) -> int | None:
    """Convert an ISO-8601 startTime (e.g. '2026-07-04T15:40:00.000Z') to epoch."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _single_book_odds(fixture: dict) -> dict | None:
    """Return the one bookmaker's odds object from a fixture (Pinnacle here)."""
    return next(iter((fixture.get("bookmakerOdds") or {}).values()), None)


def _is_real_tennis(fx: dict) -> bool:
    """Exclude simulated-reality (SRL) and virtual tennis: Pinnacle never prices
    them, so tracking them only wastes API calls."""
    text = f"{fx.get('categoryName') or ''} {fx.get('tournamentName') or ''}".lower()
    return not ("simulated" in text or "virtual" in text or "srl " in text)


def _pinnacle_h2h(book_odds: dict) -> dict | None:
    """Parse Pinnacle's full-match winner (moneyline) market.

    The match winner is the 2-way moneyline whose bookmakerMarketId ends
    "/0/moneyline"; outcomes are "home" (participant1) and "away" (participant2).
    """
    for market_id, market in (book_odds.get("markets") or {}).items():
        if market.get("marketActive") is False:
            continue
        if not (market.get("bookmakerMarketId") or "").endswith("/0/moneyline"):
            continue
        sides: dict[str, dict] = {}
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            player = (outcome.get("players") or {}).get("0") or {}
            price = player.get("price")
            if price is None or player.get("active") is False:
                continue
            boid = player.get("bookmakerOutcomeId")
            if boid in ("home", "away"):
                sides[boid] = {"outcome_id": outcome_id, "price": float(price)}
        if "home" in sides and "away" in sides:
            return {
                "market_id": market_id,
                "home_id": sides["home"]["outcome_id"],
                "away_id": sides["away"]["outcome_id"],
                "home_price": sides["home"]["price"],
                "away_price": sides["away"]["price"],
            }
    return None


def _pinnacle_main_total(book_odds: dict) -> dict | None:
    """Parse Pinnacle's main-line total-games market (players.0.mainLine == true)."""
    for market_id, market in (book_odds.get("markets") or {}).items():
        if market.get("marketActive") is False:
            continue
        sides: dict[str, dict] = {}
        is_main = False
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            player = (outcome.get("players") or {}).get("0") or {}
            price = player.get("price")
            if price is None or player.get("active") is False:
                continue
            m = _TOTALS_OUTCOME_RE.match(player.get("bookmakerOutcomeId") or "")
            if not m:
                continue
            line = float(m.group(1))
            if line < MIN_GAMES_LINE:
                continue
            if player.get("mainLine"):
                is_main = True
            sides[m.group(2)] = {"outcome_id": outcome_id, "price": float(price), "line": line}
        if is_main and "over" in sides and "under" in sides:
            return {
                "market_id": market_id,
                "line": sides["over"]["line"],
                "over_id": sides["over"]["outcome_id"],
                "under_id": sides["under"]["outcome_id"],
                "over_price": sides["over"]["price"],
                "under_price": sides["under"]["price"],
            }
    return None


def _tracked_selections(pin_book: dict, fx_meta: dict) -> list[dict]:
    """Pinnacle selections to watch for drops: H2H (both players) + main total O/U."""
    out: list[dict] = []
    h2h = _pinnacle_h2h(pin_book)
    if h2h:
        p1 = fx_meta.get("participant1Name") or "1"
        p2 = fx_meta.get("participant2Name") or "2"
        out.append({"market_id": h2h["market_id"], "outcome_id": h2h["home_id"],
                    "market_name": H2H_MARKET_NAME, "label": p1, "price": h2h["home_price"]})
        out.append({"market_id": h2h["market_id"], "outcome_id": h2h["away_id"],
                    "market_name": H2H_MARKET_NAME, "label": p2, "price": h2h["away_price"]})
    main = _pinnacle_main_total(pin_book)
    if main:
        out.append({"market_id": main["market_id"], "outcome_id": main["over_id"],
                    "market_name": TOTALS_MARKET_NAME, "label": f"Over {main['line']}",
                    "price": main["over_price"]})
        out.append({"market_id": main["market_id"], "outcome_id": main["under_id"],
                    "market_name": TOTALS_MARKET_NAME, "label": f"Under {main['line']}",
                    "price": main["under_price"]})
    return out


class TennisMonitor:
    def __init__(self, db, drop_threshold: float = DEFAULT_DROP_THRESHOLD):
        self.db = db
        self.oddspapi = OddsPapiClient()
        self.telegram = TelegramClient()
        self.drop_threshold = drop_threshold
        self.tracking_enabled = True
        self._lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}

    async def close(self):
        await self.oddspapi.close()

    async def load_settings(self):
        cfg = await self.db.settings.find_one({"_id": "config"})
        if cfg:
            self.drop_threshold = float(cfg.get("drop_threshold", self.drop_threshold))
            if "tracking_enabled" in cfg:
                self.tracking_enabled = bool(cfg["tracking_enabled"])
            token = cfg.get("telegram_token")
            chat_id = cfg.get("telegram_chat_id")
            if token:
                self.telegram.token = token
            if chat_id:
                self.telegram.chat_id = chat_id

    async def save_settings(self, drop_threshold: float | None = None,
                            tracking_enabled: bool | None = None,
                            telegram_token: str | None = None,
                            telegram_chat_id: str | None = None):
        update: dict[str, Any] = {}
        if drop_threshold is not None:
            self.drop_threshold = float(drop_threshold)
            update["drop_threshold"] = self.drop_threshold
        if tracking_enabled is not None:
            self.tracking_enabled = bool(tracking_enabled)
            update["tracking_enabled"] = self.tracking_enabled
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

    async def set_tracking(self, enabled: bool) -> bool:
        await self.save_settings(tracking_enabled=enabled)
        return self.tracking_enabled

    async def scan_once(self, force: bool = False, dry_run_notify: bool = False) -> dict:
        async with self._lock:
            started = _now()
            if not self.tracking_enabled and not force:
                self.last_scan_at = started
                self.last_scan_stats = {"skipped": True, "tracking_enabled": False}
                return self.last_scan_stats
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
        now_dt = _now()
        now_ts = int(now_dt.timestamp())
        end_ts = now_ts + WINDOW_SECONDS

        # 1) Fresh schedule of matches starting in the window (names embedded).
        schedule = await self.oddspapi.get_fixtures(now_ts, end_ts)
        window_meta: list[dict] = []
        for fx in schedule:
            if not fx.get("hasOdds") or not _is_real_tennis(fx):
                continue
            start_epoch = _parse_start_epoch(fx.get("startTime"))
            if start_epoch is None or not (now_ts < start_epoch <= end_ts):
                continue
            fx["_startEpoch"] = start_epoch
            window_meta.append(fx)

        fixture_meta_by_id = {fx["fixtureId"]: fx for fx in window_meta}
        relevant_ids = sorted({fx["tournamentId"] for fx in window_meta})

        # 2) Pinnacle odds only (no soft books) for those tournaments.
        pin_fixtures = (
            await self.oddspapi.get_odds_by_tournaments(SHARP_BOOK, relevant_ids)
            if relevant_ids else []
        )
        pin_by_id = {
            f["fixtureId"]: f for f in pin_fixtures
            if f["fixtureId"] in fixture_meta_by_id
        }

        matches_payload: list[dict] = []
        drops_found = 0
        alerts_sent = 0

        for fx_meta in window_meta:
            fixture_id = fx_meta["fixtureId"]
            pin_fx = pin_by_id.get(fixture_id)
            pin_book = _single_book_odds(pin_fx) if pin_fx else None
            if not pin_book or pin_book.get("suspended"):
                continue

            line_rows: list[dict] = []
            for sel in _tracked_selections(pin_book, fx_meta):
                key = f"{fixture_id}:{sel['market_id']}:{sel['outcome_id']}"
                prev = await self.db.line_state.find_one({"_id": key})
                curr = sel["price"]
                open_price = curr
                first_seen = now_dt.isoformat()
                drop_last = 0.0
                is_drop = False
                prev_price = None

                if prev:
                    open_price = prev.get("open_price") or prev.get("price") or curr
                    first_seen = prev.get("first_seen_at") or first_seen
                    prev_price = prev.get("price")
                    fresh = True
                    prev_at = prev.get("updated_at")
                    if prev_at:
                        prev_epoch = _parse_start_epoch(prev_at)
                        if prev_epoch is not None:
                            fresh = (now_ts - prev_epoch) <= MAX_BASELINE_AGE_SECONDS
                    if prev_price and curr < prev_price:
                        drop_last = (prev_price - curr) / prev_price
                        if fresh and drop_last >= self.drop_threshold:
                            is_drop = True

                drop_from_open = (
                    (open_price - curr) / open_price
                    if open_price and curr < open_price else 0.0
                )

                await self.db.line_state.update_one(
                    {"_id": key},
                    {"$set": {
                        "price": curr,
                        "open_price": open_price,
                        "first_seen_at": first_seen,
                        "updated_at": now_dt.isoformat(),
                        "fixture_id": fixture_id,
                    }},
                    upsert=True,
                )

                if is_drop:
                    drops_found += 1
                    text = self._format_drop_alert(
                        fx_meta, sel, prev_price, curr, drop_last, drop_from_open
                    )
                    tg_result = {"ok": False}
                    if not dry_run_notify:
                        try:
                            tg_result = await self.telegram.send_message(text)
                        except Exception as e:
                            tg_result = {"ok": False, "error": str(e)}
                    await self.db.alerts.insert_one({
                        "_id": str(uuid.uuid4()),
                        "type": "drop",
                        "created_at": now_dt.isoformat(),
                        "fixture_id": fixture_id,
                        "player1": fx_meta.get("participant1Name"),
                        "player2": fx_meta.get("participant2Name"),
                        "tournament": fx_meta.get("tournamentName"),
                        "market_name": sel["market_name"],
                        "label": sel["label"],
                        "prev_price": round(prev_price, 3) if prev_price else None,
                        "price": round(curr, 3),
                        "drop_last": round(drop_last, 4),
                        "drop_from_open": round(drop_from_open, 4),
                        "telegram_ok": bool(tg_result.get("ok")),
                        "telegram_response": tg_result,
                        "message": text,
                    })
                    alerts_sent += 1

                line_rows.append({
                    "market_name": sel["market_name"],
                    "label": sel["label"],
                    "market_id": sel["market_id"],
                    "price": round(curr, 3),
                    "open_price": round(open_price, 3),
                    "drop_from_open": round(drop_from_open, 4),
                    "drop_last": round(drop_last, 4),
                    "is_drop": is_drop,
                })

            if line_rows:
                matches_payload.append({
                    "fixture_id": fixture_id,
                    "start_time": fx_meta.get("_startEpoch"),
                    "tournament": fx_meta.get("tournamentName"),
                    "player1": fx_meta.get("participant1Name") or fx_meta.get("participant1Id"),
                    "player2": fx_meta.get("participant2Name") or fx_meta.get("participant2Id"),
                    "lines": line_rows,
                })

        # Prune stale tracked-line state (matches already started).
        cutoff = (now_dt - timedelta(seconds=LINE_STATE_TTL_SECONDS)).isoformat()
        try:
            await self.db.line_state.delete_many({"updated_at": {"$lt": cutoff}})
        except Exception:
            logger.debug("line_state prune skipped")

        await self.db.snapshots.update_one(
            {"_id": "latest"},
            {"$set": {
                "updated_at": now_dt.isoformat(),
                "drop_threshold": self.drop_threshold,
                "tracking_enabled": self.tracking_enabled,
                "matches": matches_payload,
            }},
            upsert=True,
        )

        return {
            "fixtures_tracked": len(matches_payload),
            "selections_tracked": sum(len(m["lines"]) for m in matches_payload),
            "drops_found": drops_found,
            "alerts_sent": alerts_sent,
        }

    def _format_drop_alert(self, fx_meta: dict, sel: dict, prev_price: float,
                           curr: float, drop_last: float, drop_from_open: float) -> str:
        start_ts = fx_meta.get("_startEpoch")
        start_str = ""
        if start_ts:
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M UTC")
        p1 = fx_meta.get("participant1Name") or "1"
        p2 = fx_meta.get("participant2Name") or "2"
        return (
            f"<b>⬇️ PINNACLE DROP — Tennis</b>\n"
            f"{p1} vs {p2}\n"
            f"{fx_meta.get('tournamentName') or ''} · start {start_str}\n"
            f"{sel['market_name']} — <b>{sel['label']}</b>\n"
            f"{prev_price:.2f} → <b>{curr:.2f}</b> (<b>-{drop_last * 100:.1f}%</b>)\n"
            f"da apertura: -{drop_from_open * 100:.1f}%"
        )
