"""Tennis Pinnacle line-drop monitor - core scan logic (The Odds API).

Tracks Pinnacle (sharp) prices on the Match Winner (H2H) and Total Games markets
for tennis matches starting soon, and fires a Telegram alert when a price drops
by more than the configured threshold between two scans (a "steam" move).

Rationale: when Pinnacle drops, sharp money has moved. Slow soft books (especially
the Italian ones) still show the old, higher price for a while, so there is time
to act. Only Pinnacle is read from the odds payload.

Tracking can be turned on/off at runtime (dashboard toggle); while off, scans are
skipped and no API credits are spent.

Data source: The Odds API v4. Each event carries `id`, `sport_title`,
`commence_time`, `home_team`, `away_team` and `bookmakers[]`; the Pinnacle
bookmaker exposes `markets[]` keyed "h2h" (outcomes named by team) and "totals"
(outcomes "Over"/"Under" with a `point` line).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from theoddsapi_client import TheOddsApiClient
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

SHARP_BOOK = "pinnacle"

# Only track matches starting within the next hour.
WINDOW_SECONDS = 60 * 60

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
    """Convert an ISO-8601 timestamp to epoch seconds."""
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


def _pinnacle_bookmaker(event: dict) -> dict | None:
    for b in event.get("bookmakers") or []:
        if b.get("key") == SHARP_BOOK:
            return b
    return None


def _tracked_selections(event: dict) -> list[dict]:
    """Pinnacle selections to watch: H2H (both players) + Total Games (Over/Under)."""
    book = _pinnacle_bookmaker(event)
    if not book:
        return []
    out: list[dict] = []
    for market in book.get("markets") or []:
        key = market.get("key")
        if key == "h2h":
            for o in market.get("outcomes") or []:
                price, name = o.get("price"), o.get("name")
                if price is None or not name:
                    continue
                out.append({
                    "market_key": "h2h",
                    "market_name": H2H_MARKET_NAME,
                    "outcome": name,
                    "point": None,
                    "label": name,
                    "price": float(price),
                })
        elif key == "totals":
            for o in market.get("outcomes") or []:
                price, name, point = o.get("price"), o.get("name"), o.get("point")
                if price is None or not name:
                    continue
                out.append({
                    "market_key": "totals",
                    "market_name": TOTALS_MARKET_NAME,
                    "outcome": name,
                    "point": point,
                    "label": f"{name} {point}" if point is not None else name,
                    "price": float(price),
                })
    return out


class TennisMonitor:
    def __init__(self, db, drop_threshold: float = DEFAULT_DROP_THRESHOLD):
        self.db = db
        self.client = TheOddsApiClient()
        self.telegram = TelegramClient()
        self.drop_threshold = drop_threshold
        self.tracking_enabled = True
        self._lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}

    async def close(self):
        await self.client.close()

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

        events = await self.client.get_tennis_events()
        window: list[dict] = []
        for ev in events:
            start_epoch = _parse_start_epoch(ev.get("commence_time"))
            if start_epoch is None or not (now_ts < start_epoch <= end_ts):
                continue
            ev["_startEpoch"] = start_epoch
            window.append(ev)

        matches_payload: list[dict] = []
        drops_found = 0
        alerts_sent = 0

        for ev in window:
            event_id = ev.get("id")
            selections = _tracked_selections(ev)
            if not selections:
                continue

            line_rows: list[dict] = []
            for sel in selections:
                point_key = "" if sel["point"] is None else sel["point"]
                key = f"{event_id}:{sel['market_key']}:{sel['outcome']}:{point_key}"
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
                    prev_epoch = _parse_start_epoch(prev.get("updated_at"))
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
                        "event_id": event_id,
                    }},
                    upsert=True,
                )

                if is_drop:
                    drops_found += 1
                    text = self._format_drop_alert(
                        ev, sel, prev_price, curr, drop_last, drop_from_open
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
                        "player1": ev.get("home_team"),
                        "player2": ev.get("away_team"),
                        "tournament": ev.get("sport_title"),
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
                    "price": round(curr, 3),
                    "open_price": round(open_price, 3),
                    "drop_from_open": round(drop_from_open, 4),
                    "drop_last": round(drop_last, 4),
                    "is_drop": is_drop,
                })

            if line_rows:
                matches_payload.append({
                    "event_id": event_id,
                    "start_time": ev.get("_startEpoch"),
                    "tournament": ev.get("sport_title"),
                    "player1": ev.get("home_team"),
                    "player2": ev.get("away_team"),
                    "lines": line_rows,
                })

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
            "requests_remaining": self.client.requests_remaining,
        }

    def _format_drop_alert(self, ev: dict, sel: dict, prev_price: float,
                           curr: float, drop_last: float, drop_from_open: float) -> str:
        start_ts = ev.get("_startEpoch")
        start_str = ""
        if start_ts:
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M UTC")
        return (
            f"<b>⬇️ PINNACLE DROP — Tennis</b>\n"
            f"{ev.get('home_team')} vs {ev.get('away_team')}\n"
            f"{ev.get('sport_title') or ''} · start {start_str}\n"
            f"{sel['market_name']} — <b>{sel['label']}</b>\n"
            f"{prev_price:.2f} → <b>{curr:.2f}</b> (<b>-{drop_last * 100:.1f}%</b>)\n"
            f"da apertura: -{drop_from_open * 100:.1f}%"
        )
