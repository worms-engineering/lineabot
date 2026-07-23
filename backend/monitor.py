"""Tennis Pinnacle line-drop monitor - provider-agnostic core.

Tracks Pinnacle (sharp) prices on the Match Winner (H2H) and Total Games markets
for tennis matches starting soon, and fires a Telegram alert when a price drops
by more than the configured threshold between two scans (a "steam" move).

The odds source is pluggable: `provider` selects between The Odds API
(major tournaments, clean data, credit-based) and OddsPapi (full calendar incl.
Challenger/ITF). Each provider client returns the same normalized shape via
get_pinnacle_matches(); everything below is provider-agnostic.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from theoddsapi_client import TheOddsApiClient
from oddspapi_client import OddsPapiClient
from telegram_client import TelegramClient

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60 * 60
# Ignore matches about to start: leaves time to act and avoids tracking/alerting
# a match that will have kicked off by the time you see it.
MIN_LEAD_SECONDS = 90
DEFAULT_DROP_THRESHOLD = 0.05
MAX_BASELINE_AGE_SECONDS = 30 * 60
LINE_STATE_TTL_SECONDS = 6 * 60 * 60

DEFAULT_PROVIDER = "theoddsapi"

# Basketball is tracked only on OddsPapi (the provider with basketball parsing),
# restricted to these competitions (tournament-name substrings, case-insensitive):
# NBA + NBA Summer League + WNBA + EuroBasket. Edit to taste.
BASKETBALL_WHITELIST = ["nba", "wnba", "eurobasket"]

# Football whitelist for OddsPapi (worldwide calendar, so plain substrings
# would false-match: "bundesliga" also sits inside "2. Bundesliga", "laliga"
# inside "LaLiga2", etc). Domestic top flights use EXACT tournament-name
# matches (2-tuples); UEFA cups use substring matches (3-tuples, trailing
# "contains") so qualifying-round variants like "UEFA Europa League
# Qualification" are still caught. Edit to taste — check real names via
# GET /v4/tournaments?sportId=10 since OddsPapi's exact spelling may differ.
FOOTBALL_WHITELIST_ODDSPAPI = [
    ("england", "premier league"),
    ("spain", "laliga"),
    ("spain", "la liga"),
    ("italy", "serie a"),
    ("germany", "bundesliga"),
    ("france", "ligue 1"),
    (None, "champions league", "contains"),
    (None, "europa league", "contains"),
    (None, "europa conference league", "contains"),
]

# Football on The Odds API uses a fixed sport-key whitelist instead
# (FOOTBALL_LEAGUE_KEYS in theoddsapi_client.py), so no name-based filter here.

SPORT_META = {
    "tennis": {"label": "Tennis", "emoji": "🎾"},
    "basketball": {"label": "Basket", "emoji": "🏀"},
    "football": {"label": "Calcio", "emoji": "⚽"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_epoch(value: Any) -> int | None:
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


class TennisMonitor:
    def __init__(self, db, drop_threshold: float = DEFAULT_DROP_THRESHOLD):
        self.db = db
        self.telegram = TelegramClient()
        self.clients = {
            "theoddsapi": TheOddsApiClient(),
            "oddspapi": OddsPapiClient(),
        }
        self.provider = DEFAULT_PROVIDER
        self.football_provider = "oddspapi"  # switch to "theoddsapi" later in-season
        self.drop_threshold = drop_threshold
        self.tracking_enabled = True
        self.basketball_enabled = True
        self.football_enabled = True
        self._lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}

    @property
    def client(self):
        return self.clients[self.provider]

    async def close(self):
        for c in self.clients.values():
            await c.close()

    async def load_settings(self):
        cfg = await self.db.settings.find_one({"_id": "config"})
        if cfg:
            self.drop_threshold = float(cfg.get("drop_threshold", self.drop_threshold))
            if "tracking_enabled" in cfg:
                self.tracking_enabled = bool(cfg["tracking_enabled"])
            if "basketball_enabled" in cfg:
                self.basketball_enabled = bool(cfg["basketball_enabled"])
            if "football_enabled" in cfg:
                self.football_enabled = bool(cfg["football_enabled"])
            if cfg.get("provider") in self.clients:
                self.provider = cfg["provider"]
            if cfg.get("football_provider") in self.clients:
                self.football_provider = cfg["football_provider"]
            token = cfg.get("telegram_token")
            chat_id = cfg.get("telegram_chat_id")
            if token:
                self.telegram.token = token
            if chat_id:
                self.telegram.chat_id = chat_id

    async def save_settings(self, drop_threshold: float | None = None,
                            tracking_enabled: bool | None = None,
                            basketball_enabled: bool | None = None,
                            football_enabled: bool | None = None,
                            provider: str | None = None,
                            football_provider: str | None = None,
                            telegram_token: str | None = None,
                            telegram_chat_id: str | None = None):
        update: dict[str, Any] = {}
        if drop_threshold is not None:
            self.drop_threshold = float(drop_threshold)
            update["drop_threshold"] = self.drop_threshold
        if tracking_enabled is not None:
            self.tracking_enabled = bool(tracking_enabled)
            update["tracking_enabled"] = self.tracking_enabled
        if basketball_enabled is not None:
            self.basketball_enabled = bool(basketball_enabled)
            update["basketball_enabled"] = self.basketball_enabled
        if football_enabled is not None:
            self.football_enabled = bool(football_enabled)
            update["football_enabled"] = self.football_enabled
        if provider is not None:
            if provider not in self.clients:
                raise ValueError(f"unknown provider: {provider}")
            self.provider = provider
            update["provider"] = provider
        if football_provider is not None:
            if football_provider not in self.clients:
                raise ValueError(f"unknown football_provider: {football_provider}")
            self.football_provider = football_provider
            update["football_provider"] = football_provider
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

    async def set_provider(self, provider: str) -> str:
        await self.save_settings(provider=provider)
        return self.provider

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

    def _scan_plan(self) -> list[tuple]:
        """(sport, provider_key, tournament_whitelist) to scan this cycle."""
        plan = [("tennis", self.provider, None)]
        if self.basketball_enabled:
            plan.append(("basketball", "oddspapi", BASKETBALL_WHITELIST))
        if self.football_enabled:
            # Independent toggle from tennis' `provider`: OddsPapi needs a
            # name-based whitelist (worldwide calendar), The Odds API filters
            # via its own fixed sport-key list internally (whitelist=None).
            whitelist = FOOTBALL_WHITELIST_ODDSPAPI if self.football_provider == "oddspapi" else None
            plan.append(("football", self.football_provider, whitelist))
        return plan

    async def _scan_impl(self, dry_run_notify: bool) -> dict:
        now_dt = _now()
        now_ts = int(now_dt.timestamp())
        window_start = now_ts + MIN_LEAD_SECONDS  # skip matches about to start
        end_ts = now_ts + WINDOW_SECONDS

        matches: list[dict] = []
        sport_errors: dict[str, str] = {}
        for sport, prov, whitelist in self._scan_plan():
            client = self.clients[prov]
            try:
                raw = await client.get_pinnacle_matches(sport, window_start, end_ts, whitelist)
            except Exception as e:
                logger.warning("scan sport=%s provider=%s failed: %s", sport, prov, e)
                sport_errors[sport] = str(e)
                continue
            for m in raw:
                m["sport"] = sport
                m["provider"] = prov
                matches.append(m)

        matches_payload: list[dict] = []
        drops_found = 0
        alerts_sent = 0

        for match in matches:
            match_id = match.get("match_id")
            provider = match.get("provider", self.provider)
            sport = match.get("sport", "tennis")
            line_rows: list[dict] = []
            for sel in match.get("selections") or []:
                point_key = "" if sel.get("point") is None else sel["point"]
                key = f"{provider}:{sport}:{match_id}:{sel['market_key']}:{sel['outcome']}:{point_key}"
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
                    prev_epoch = _parse_iso_epoch(prev.get("updated_at"))
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
                        "match_id": match_id,
                    }},
                    upsert=True,
                )

                if is_drop:
                    drops_found += 1
                    text = self._format_drop_alert(match, sel, prev_price, curr,
                                                   drop_last, drop_from_open)
                    tg_result = {"ok": False}
                    if not dry_run_notify:
                        try:
                            tg_result = await self.telegram.send_message(text)
                        except Exception as e:
                            tg_result = {"ok": False, "error": str(e)}
                    await self.db.alerts.insert_one({
                        "_id": str(uuid.uuid4()),
                        "type": "drop",
                        "provider": provider,
                        "sport": sport,
                        "created_at": now_dt.isoformat(),
                        "player1": match.get("player1"),
                        "player2": match.get("player2"),
                        "tournament": match.get("tournament"),
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
                meta = SPORT_META.get(sport, {})
                matches_payload.append({
                    "match_id": match_id,
                    "sport": sport,
                    "sport_label": meta.get("label", sport),
                    "sport_emoji": meta.get("emoji", ""),
                    "start_time": match.get("start_epoch"),
                    "tournament": match.get("tournament"),
                    "player1": match.get("player1"),
                    "player2": match.get("player2"),
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
                "provider": self.provider,
                "basketball_enabled": self.basketball_enabled,
                "football_enabled": self.football_enabled,
                "football_provider": self.football_provider,
                "drop_threshold": self.drop_threshold,
                "tracking_enabled": self.tracking_enabled,
                "matches": matches_payload,
            }},
            upsert=True,
        )

        stats = {
            "provider": self.provider,
            "fixtures_tracked": len(matches_payload),
            "selections_tracked": sum(len(m["lines"]) for m in matches_payload),
            "drops_found": drops_found,
            "alerts_sent": alerts_sent,
            "requests_remaining": self.client.requests_remaining,
        }
        if sport_errors:
            stats["sport_errors"] = sport_errors
        return stats

    def _format_drop_alert(self, match: dict, sel: dict, prev_price: float,
                           curr: float, drop_last: float, drop_from_open: float) -> str:
        start_ts = match.get("start_epoch")
        start_str = ""
        if start_ts:
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M UTC")
        meta = SPORT_META.get(match.get("sport"), {})
        sport_tag = f"{meta.get('emoji', '')} {meta.get('label', 'Tennis')}".strip()
        return (
            f"<b>⬇️ PINNACLE DROP — {sport_tag}</b>\n"
            f"{match.get('player1')} vs {match.get('player2')}\n"
            f"{match.get('tournament') or ''} · start {start_str}\n"
            f"{sel['market_name']} — <b>{sel['label']}</b>\n"
            f"{prev_price:.2f} → <b>{curr:.2f}</b> (<b>-{drop_last * 100:.1f}%</b>)\n"
            f"da apertura: -{drop_from_open * 100:.1f}%"
        )
