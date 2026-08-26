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
import html
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import UpdateOne

from theoddsapi_client import TheOddsApiClient
from oddspapi_client import OddsPapiClient
from telegram_client import TelegramClient
from key_rotation import generate_oddspapi_key, KeyRotationError
import prediction_markets as pmk
from prediction_markets import PredictionMarketsClient

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60 * 60
# Ignore matches about to start: leaves time to act and avoids tracking/alerting
# a match that will have kicked off by the time you see it.
MIN_LEAD_SECONDS = 90
DEFAULT_DROP_THRESHOLD = 0.05
MAX_BASELINE_AGE_SECONDS = 30 * 60
LINE_STATE_TTL_SECONDS = 6 * 60 * 60
# The scans collection is a write-only audit log; auto-expire it so the fast
# prediction-market loop (a doc/minute) can't grow it without bound.
SCANS_TTL_SECONDS = 7 * 24 * 60 * 60

# Minimum time between auto key-rotation attempts, so a broken generator
# (site down, signup captcha...) doesn't get hammered every scan.
ROTATION_COOLDOWN_SECONDS = 5 * 60

# Tennis, basketball and football all run on OddsPapi (full calendar). The Odds
# API stays available as a switchable provider (and is still used for the
# football best-Italian-price / Betfair cross-check), but is no longer the
# default odds source for any sport.
DEFAULT_PROVIDER = "oddspapi"

# Basketball is tracked only on OddsPapi (the provider with basketball parsing),
# restricted to these competitions (tournament-name substrings, case-insensitive):
# NBA + NBA Summer League + WNBA + EuroBasket. Edit to taste.
BASKETBALL_WHITELIST = ["nba", "wnba", "eurobasket"]

# Hockey runs on OddsPapi. Left open (None): the hockey calendar is small
# enough (even in-season a 1h window sees a handful of leagues) and Pinnacle
# prices it densely, including minor leagues. Restrict with exact 2-tuples
# like football if the off-season friendlies get noisy.
HOCKEY_WHITELIST = None

# Volleyball runs on OddsPapi, H2H-only (see H2H_ONLY_SPORTS in
# oddspapi_client.py). The worldwide calendar is huge (289 tournaments) and
# mostly leagues no Italian book prices, so - like football - restrict to the
# competitions Italian soft books actually cover: the domestic top flights +
# cup, the CEV club cups, and the senior national-team events (men & women).
# Names verified live against GET /v4/tournaments?sportId=23. Exact 2-tuple
# matches (optionally category-scoped) to avoid pulling age-group / continental
# / qualification variants that share a word.
VOLLEY_WHITELIST_ODDSPAPI = [
    ("italy", "superlega"),
    ("italy", "coppa italia superlega"),
    ("italy", "serie a1 women"),
    ("international", "champions league"),          # men CEV Champions League
    ("international", "cev champions league, women"),
    ("international", "cev cup"),
    ("international", "cev cup, women"),
    ("international", "fivb world championship"),
    ("international", "fivb world championship women"),
    ("international", "nations league"),            # VNL men
    ("international", "nations league, women"),      # VNL women
    ("international", "european championship"),      # EuroVolley men
    ("international", "cev eurovolley, women"),       # EuroVolley women
    ("international", "olympic tournament"),
    ("international", "olympic tournament women"),
    ("international", "world cup"),
    ("international", "world cup women"),
]

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
    ("italy", "coppa italia"),  # exact match: excludes "Coppa Italia Serie C"
    ("germany", "bundesliga"),
    ("france", "ligue 1"),
    ("netherlands", "eredivisie"),  # exact match: excludes "Eredivisie SRL" (virtual)
    ("portugal", "liga portugal"),  # exact match: excludes "Liga Portugal 2"/"3"
    (None, "champions league", "contains"),
    (None, "europa league", "contains"),
    (None, "conference league", "contains"),
    # Amichevoli: OddsPapi bundles ALL club friendlies worldwide under this one
    # tournament name (from top-club preseason tours down to reserve/lower-league
    # sides), no way to narrow it further by name - if it's too much noise, raise
    # football_drop_threshold rather than trying to filter here.
    (None, "friendly", "contains"),
]

# Football on The Odds API uses a fixed sport-key whitelist instead
# (FOOTBALL_LEAGUE_KEYS in theoddsapi_client.py), so no name-based filter here.

SPORT_META = {
    "tennis": {"label": "Tennis", "emoji": "🎾"},
    "basketball": {"label": "Basket", "emoji": "🏀"},
    "football": {"label": "Calcio", "emoji": "⚽"},
    "hockey": {"label": "Hockey", "emoji": "🏒"},
    "volley": {"label": "Volley", "emoji": "🏐"},
    "f1": {"label": "Formula 1", "emoji": "🏎️"},
    "mlb": {"label": "MLB", "emoji": "⚾"},
    "outright": {"label": "Outright", "emoji": "🏆"},
}

# Sports sourced from keyless prediction markets: tracked on the fast loop
# (scanned every F1_REFRESH_SECONDS instead of every REFRESH_MINUTES). They use
# the same 60-minute pre-match window as every other sport.
# Limited to F1 and MLB (baseball); UFC was dropped as no Italian book prices it.
PREDICTION_SPORTS = ("f1", "mlb")

# Outright (tournament-winner / award) markets on Polymarket, tracked on the
# MAIN scan loop and - unlike everything else - with NO 60-minute window: an
# outright is followed for its whole life, from when the market opens until it
# closes/resolves (months). Each entry pins a Polymarket tag (`tag`) and, when
# the tag is broad, a lowercase title substring (`title`) to select the right
# event; the contender list is built from that event's 'Will <X> win ...?'
# sub-markets. Restricted to competitions Italian books price. Edit to taste;
# names/tags verified live against the Polymarket Gamma API.
OUTRIGHT_MARKETS = [
    {"emoji": "⚽", "tag": "champions-league", "title": "champion"},
    {"emoji": "⚽", "tag": "epl", "title": "champion"},
    {"emoji": "⚽", "tag": "la-liga", "title": "champion"},
    {"emoji": "⚽", "tag": "serie-a", "title": "champion"},
    {"emoji": "⚽", "tag": "ligue-1", "title": "champion"},
    {"emoji": "🏆", "tag": "soccer", "title": "ballon"},   # Pallone d'Oro
    {"emoji": "🎾", "tag": "tennis", "title": "winner"},    # Slam winners
    # Golf: the tag pulls in thin minor tours (LPGA/DP World secondary events)
    # that flooded alerts - require a deeper book so only majors + flagship
    # events (Tour Championship, majors) survive.
    {"emoji": "⛳", "tag": "golf", "title": "winner", "min_liquidity": 250_000},
]

PROVIDER_LABELS = {"theoddsapi": "The Odds API", "oddspapi": "OddsPapi"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _line_key(provider: str, sport: str, match_id: Any, sel: dict) -> str:
    """Stable per-selection line_state document id. Kept in one place so the
    batched pre-fetch and the per-selection update below can never drift."""
    point_key = "" if sel.get("point") is None else sel["point"]
    return f"{provider}:{sport}:{match_id}:{sel['market_key']}:{sel['outcome']}:{point_key}"


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
            "prediction": PredictionMarketsClient(),
        }
        self.provider = DEFAULT_PROVIDER
        self.football_provider = "oddspapi"  # switch to "theoddsapi" later in-season
        self.drop_threshold = drop_threshold
        # Football has many more markets/books moving at once than tennis or
        # basketball, which was flooding alerts at the same 5% default -
        # independently configurable, defaults to the same value as tennis/basket.
        self.football_drop_threshold = drop_threshold
        self.tracking_enabled = True
        self.basketball_enabled = True
        self.football_enabled = True
        self.hockey_enabled = True
        self.volley_enabled = True
        self.f1_enabled = True
        self.mlb_enabled = True
        self.outright_enabled = True
        self._lock = asyncio.Lock()
        self.last_scan_at: datetime | None = None
        self.last_scan_error: str | None = None
        self.last_scan_stats: dict[str, Any] = {}
        # Providers we've already sent a "quota exhausted" Telegram alert for,
        # so we don't re-alert on every scan while it stays exhausted.
        self._quota_alerted: set[str] = set()
        self._ipblock_alerted: set[str] = set()
        # Auto key-rotation state (OddsPapi only; The Odds API can't self-serve).
        self._last_rotation_attempt: datetime | None = None
        self.last_rotation_at: datetime | None = None
        self.last_rotation_error: str | None = None

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
            self.football_drop_threshold = float(
                cfg.get("football_drop_threshold", self.football_drop_threshold)
            )
            if "tracking_enabled" in cfg:
                self.tracking_enabled = bool(cfg["tracking_enabled"])
            if "basketball_enabled" in cfg:
                self.basketball_enabled = bool(cfg["basketball_enabled"])
            if "football_enabled" in cfg:
                self.football_enabled = bool(cfg["football_enabled"])
            if "hockey_enabled" in cfg:
                self.hockey_enabled = bool(cfg["hockey_enabled"])
            if "volley_enabled" in cfg:
                self.volley_enabled = bool(cfg["volley_enabled"])
            if "f1_enabled" in cfg:
                self.f1_enabled = bool(cfg["f1_enabled"])
            if "mlb_enabled" in cfg:
                self.mlb_enabled = bool(cfg["mlb_enabled"])
            if "outright_enabled" in cfg:
                self.outright_enabled = bool(cfg["outright_enabled"])
            if cfg.get("provider") in self.clients:
                self.provider = cfg["provider"]
            if cfg.get("football_provider") in self.clients:
                self.football_provider = cfg["football_provider"]
            # OddsPapi key: a key stored here (set from the dashboard, or written
            # by auto-rotation) takes precedence and wins. The ODDSPAPI_KEY env
            # var, applied in the client's __init__, is only the initial seed used
            # until a key is saved to the DB - so you can rotate the key from the
            # dashboard (mobile included) without touching the host or redeploying.
            oddspapi_key = cfg.get("oddspapi_api_key")
            if oddspapi_key:
                self.clients["oddspapi"].api_key = oddspapi_key
            token = cfg.get("telegram_token")
            chat_id = cfg.get("telegram_chat_id")
            if token:
                self.telegram.token = token
            if chat_id:
                self.telegram.chat_id = chat_id

    async def ensure_indexes(self):
        """Create the indexes the hot paths rely on. Best-effort and
        idempotent (safe to call on every startup):
        - alerts.created_at desc: /api/alerts sorts on it;
        - line_state.updated_at: the end-of-scan stale-row prune range-deletes
          on it;
        - scans.created_at TTL: the scans audit log is write-only and, with the
          fast prediction-market loop, grows by ~1 doc/minute - expire it so it
          can't balloon unbounded.
        """
        try:
            await self.db.alerts.create_index([("created_at", -1)])
            await self.db.line_state.create_index("updated_at")
            await self.db.scans.create_index(
                "created_at", expireAfterSeconds=SCANS_TTL_SECONDS)
        except Exception:
            logger.exception("index creation failed (non-fatal)")

    async def save_settings(self, drop_threshold: float | None = None,
                            football_drop_threshold: float | None = None,
                            tracking_enabled: bool | None = None,
                            basketball_enabled: bool | None = None,
                            football_enabled: bool | None = None,
                            hockey_enabled: bool | None = None,
                            volley_enabled: bool | None = None,
                            f1_enabled: bool | None = None,
                            mlb_enabled: bool | None = None,
                            outright_enabled: bool | None = None,
                            provider: str | None = None,
                            football_provider: str | None = None,
                            telegram_token: str | None = None,
                            telegram_chat_id: str | None = None,
                            oddspapi_api_key: str | None = None):
        update: dict[str, Any] = {}
        if drop_threshold is not None:
            self.drop_threshold = float(drop_threshold)
            update["drop_threshold"] = self.drop_threshold
        if football_drop_threshold is not None:
            self.football_drop_threshold = float(football_drop_threshold)
            update["football_drop_threshold"] = self.football_drop_threshold
        if tracking_enabled is not None:
            self.tracking_enabled = bool(tracking_enabled)
            update["tracking_enabled"] = self.tracking_enabled
        if basketball_enabled is not None:
            self.basketball_enabled = bool(basketball_enabled)
            update["basketball_enabled"] = self.basketball_enabled
        if football_enabled is not None:
            self.football_enabled = bool(football_enabled)
            update["football_enabled"] = self.football_enabled
        if hockey_enabled is not None:
            self.hockey_enabled = bool(hockey_enabled)
            update["hockey_enabled"] = self.hockey_enabled
        if volley_enabled is not None:
            self.volley_enabled = bool(volley_enabled)
            update["volley_enabled"] = self.volley_enabled
        if f1_enabled is not None:
            self.f1_enabled = bool(f1_enabled)
            update["f1_enabled"] = self.f1_enabled
        if mlb_enabled is not None:
            self.mlb_enabled = bool(mlb_enabled)
            update["mlb_enabled"] = self.mlb_enabled
        if outright_enabled is not None:
            self.outright_enabled = bool(outright_enabled)
            update["outright_enabled"] = self.outright_enabled
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
        if oddspapi_api_key is not None:
            self.clients["oddspapi"].api_key = oddspapi_api_key
            update["oddspapi_api_key"] = oddspapi_api_key
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

    async def _maybe_rotate_oddspapi_key(self):
        """Swap in a freshly generated OddsPapi key when the current one's
        quota is exhausted. Cooldown-limited; notifies via Telegram on both
        success and (once per outage) failure so silent coverage loss is
        impossible. Disabled unless ODDSPAPI_AUTO_ROTATE=true: the automated
        signups from a datacenter IP are what got the original host blocked,
        so keys are changed by hand by default."""
        if os.environ.get("ODDSPAPI_AUTO_ROTATE", "false").lower() != "true":
            return
        oddspapi = self.clients["oddspapi"]
        if not oddspapi.quota_exhausted:
            return
        now = _now()
        if (self._last_rotation_attempt is not None
                and (now - self._last_rotation_attempt).total_seconds()
                < ROTATION_COOLDOWN_SECONDS):
            return
        self._last_rotation_attempt = now
        try:
            new_key = await generate_oddspapi_key()
        except KeyRotationError as e:
            logger.error("OddsPapi key rotation failed: %s", e)
            self.last_rotation_error = str(e)
            if not getattr(self, "_rotation_fail_alerted", False):
                self._rotation_fail_alerted = True
                try:
                    await self.telegram.send_message(
                        "⚠️ <b>Quota OddsPapi esaurita</b>\n"
                        "Tentativo di rotazione automatica della key fallito:\n"
                        f"<code>{str(e)[:300]}</code>\n"
                        "Riprovo ogni 5 minuti.")
                except Exception:
                    logger.exception("rotation-failure telegram notify failed")
            return
        await self.save_settings(oddspapi_api_key=new_key)
        oddspapi.quota_exhausted = False
        self.last_rotation_at = now
        self.last_rotation_error = None
        self._rotation_fail_alerted = False
        self._quota_alerted.discard("oddspapi")
        try:
            await self.telegram.send_message(
                "🔄 <b>Key OddsPapi ruotata automaticamente</b>\n"
                "Quota esaurita rilevata: generata e attivata una nuova key "
                f"(ultima 8: <code>{new_key[-8:]}</code>).\n"
                "Nessuna interruzione di copertura necessaria.")
        except Exception:
            logger.exception("rotation telegram notify failed")
        logger.info("OddsPapi key rotated automatically")

    async def scan_once(self, force: bool = False, dry_run_notify: bool = False,
                        sports: tuple[str, ...] | None = None,
                        update_state: bool = True) -> dict:
        """One scan. `sports` restricts the scan plan (used by the fast F1
        loop); partial scans merge into the snapshot and don't clobber the
        main scan's status/stats so the dashboard keeps showing the full
        picture."""
        async with self._lock:
            started = _now()
            if not self.tracking_enabled and not force:
                if update_state:
                    self.last_scan_at = started
                    self.last_scan_stats = {"skipped": True, "tracking_enabled": False}
                return {"skipped": True, "tracking_enabled": False}
            try:
                if not dry_run_notify and sports is None:
                    # Best-effort key rotation before scanning, so a quota
                    # death costs at most one scan cycle of coverage.
                    await self._maybe_rotate_oddspapi_key()
                result = await self._scan_impl(dry_run_notify=dry_run_notify,
                                               sports=sports)
                if update_state:
                    self.last_scan_at = started
                    self.last_scan_error = None
                    self.last_scan_stats = result
                await self.db.scans.insert_one({
                    "_id": str(uuid.uuid4()),
                    "started_at": started.isoformat(),
                    "created_at": started,  # BSON date, drives the TTL index
                    "stats": result,
                    "partial": sports is not None,
                    "error": None,
                })
                return result
            except Exception as e:
                logger.exception("scan failed")
                if update_state:
                    self.last_scan_at = started
                    self.last_scan_error = str(e)
                await self.db.scans.insert_one({
                    "_id": str(uuid.uuid4()),
                    "started_at": started.isoformat(),
                    "created_at": started,  # BSON date, drives the TTL index
                    "stats": {},
                    "partial": sports is not None,
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
        if self.hockey_enabled:
            plan.append(("hockey", "oddspapi", HOCKEY_WHITELIST))
        if self.volley_enabled:
            # Volleyball: OddsPapi, H2H-only, restricted to the Italy-relevant
            # competitions (whitelist above).
            plan.append(("volley", "oddspapi", VOLLEY_WHITELIST_ODDSPAPI))
        if self.f1_enabled:
            plan.append(("f1", "prediction", None))
        if self.mlb_enabled:
            plan.append(("mlb", "prediction", None))
        return plan

    async def _scan_impl(self, dry_run_notify: bool,
                         sports: tuple[str, ...] | None = None) -> dict:
        now_dt = _now()
        now_ts = int(now_dt.timestamp())
        window_start = now_ts + MIN_LEAD_SECONDS  # skip matches about to start
        end_ts = now_ts + WINDOW_SECONDS

        matches: list[dict] = []
        sport_errors: dict[str, str] = {}
        for sport, prov, whitelist in self._scan_plan():
            if sports is not None and sport not in sports:
                continue
            client = self.clients[prov]
            # Every sport - prediction markets (F1/MLB) included - uses the same
            # 60-minute pre-match window: only events starting within the hour
            # are tracked. (Prediction sports still scan on the fast loop, so an
            # imminent race/game is polled every F1_REFRESH_SECONDS rather than
            # once per REFRESH_MINUTES.)
            sport_ws, sport_we = window_start, end_ts
            raw: list[dict] | None = None
            try:
                raw = await client.get_pinnacle_matches(sport, sport_ws, sport_we, whitelist)
            except Exception as e:
                logger.warning("scan sport=%s provider=%s failed: %s", sport, prov, e)
                sport_errors[sport] = str(e)
            # Checked on both success and failure: OddsPapi only flags
            # quota_exhausted via a raised error, but The Odds API can also
            # flip it on a *successful* call once requests_remaining hits 0.
            if getattr(client, "quota_exhausted", False):
                if not dry_run_notify:
                    await self._notify_quota_exhausted(prov)
            else:
                self._quota_alerted.discard(prov)  # quota ok/reset: re-arm the alert
            if getattr(client, "ip_blocked", False):
                if not dry_run_notify:
                    await self._notify_ip_blocked(prov)
            else:
                self._ipblock_alerted.discard(prov)
            if raw is None:
                continue
            for m in raw:
                m["sport"] = sport
                m["provider"] = prov
                matches.append(m)

        # Outrights (Polymarket): main loop only, no 60-minute window - tracked
        # from market open to close. Merged into the same drop-detection pipeline
        # as everything else (one selection per contender).
        if self.outright_enabled and (sports is None or "outright" in sports):
            try:
                raw = await self.clients["prediction"].get_outright_matches(OUTRIGHT_MARKETS)
                for m in raw:
                    m["sport"] = "outright"
                    m["provider"] = "prediction"
                    matches.append(m)
            except Exception as e:
                logger.warning("outright scan failed: %s", e)
                sport_errors["outright"] = str(e)

        matches_payload: list[dict] = []
        drops_found = 0
        alerts_sent = 0

        # Pre-pass: filter out started matches and pre-compute every
        # line_state key, so the whole scan's previous state can be read in a
        # single query instead of one find_one per selection (a busy tennis
        # scan is easily 50+ fixtures x 4 selections = 200 sequential, latency-
        # bound round-trips otherwise). Updates are likewise batched into one
        # bulk_write at the end.
        plan_items: list[dict] = []
        all_keys: list[str] = []
        for match in matches:
            start_epoch = match.get("start_epoch")
            if start_epoch is not None and start_epoch <= now_ts:
                # Match already underway: the 90s fetch-time lead is not
                # enough on its own - scans take 10-30s (rate-limited) and
                # OddsPapi shifts startTime on delays, which can push a
                # started fixture back into the window with a minutes-old
                # baseline. In-play prices would read as huge "drops", so
                # don't track or alert on it at all.
                continue
            provider = match.get("provider", self.provider)
            sport = match.get("sport", "tennis")
            threshold = self.football_drop_threshold if sport == "football" else self.drop_threshold
            match_id = match.get("match_id")
            sels: list[tuple[dict, str]] = []
            for sel in match.get("selections") or []:
                key = _line_key(provider, sport, match_id, sel)
                sels.append((sel, key))
                all_keys.append(key)
            plan_items.append({"match": match, "provider": provider, "sport": sport,
                               "threshold": threshold, "match_id": match_id, "sels": sels})

        prev_states: dict[str, dict] = {}
        if all_keys:
            async for doc in self.db.line_state.find({"_id": {"$in": all_keys}}):
                prev_states[doc["_id"]] = doc
        line_ops: list[UpdateOne] = []

        for item in plan_items:
            match = item["match"]
            match_id = item["match_id"]
            start_epoch = match.get("start_epoch")
            provider = item["provider"]
            sport = item["sport"]
            threshold = item["threshold"]
            line_rows: list[dict] = []
            for sel, key in item["sels"]:
                prev = prev_states.get(key)
                curr = sel["price"]
                open_price = curr
                first_seen = now_dt.isoformat()
                drop_last = 0.0
                is_drop = False
                prev_price = None
                observations = 1

                if prev:
                    observations = int(prev.get("observations", 1)) + 1
                    open_price = prev.get("open_price") or prev.get("price") or curr
                    first_seen = prev.get("first_seen_at") or first_seen
                    if observations >= 3:
                        prev_price = prev.get("price")
                        fresh = True
                        prev_epoch = _parse_iso_epoch(prev.get("updated_at"))
                        if prev_epoch is not None:
                            fresh = (now_ts - prev_epoch) <= MAX_BASELINE_AGE_SECONDS
                        if prev_price and curr < prev_price:
                            drop_last = (prev_price - curr) / prev_price
                    else:
                        # Still building a reliable baseline: a newly-tracked
                        # selection's very first tick can be a thin/unsettled
                        # price before the market forms (seen live: a favorite's
                        # opening quote read ~3.5, then immediately and
                        # permanently settled around ~1.5 - comparing against
                        # that first tick forever kept firing bogus alerts).
                        # Re-anchor open_price to this second observation
                        # instead of trusting the first one.
                        open_price = curr

                drop_from_open = (
                    (open_price - curr) / open_price
                    if open_price and curr < open_price else 0.0
                )

                # In-play oscillation detector: same-day (pn-feed) tennis has
                # no status flag and unreliable start times, but in-play
                # prices oscillate (drop AND bounce), while genuine prematch
                # steam is directional. A >=3% bounce once, or >=1.5% twice,
                # marks the selection suspect-live: keep tracking it, never
                # alert on it again.
                rises = int((prev or {}).get("rises", 0))
                suspect_live = bool((prev or {}).get("suspect_live"))
                if prev_price and curr > prev_price:
                    bounce = (curr - prev_price) / prev_price
                    if bounce >= 0.03:
                        rises += 2
                    elif bounce >= 0.015:
                        rises += 1
                if rises >= 2:
                    suspect_live = True

                # Require the price to actually be down from where it opened, not
                # just from whatever it happened to read last scan. Thin-liquidity
                # fixtures (e.g. lower-profile league matches) can flap a price back
                # and forth between two levels scan to scan without ever really
                # moving from open - gating on drop_from_open too kills those false
                # alerts while still catching genuine (monotonic) steam.
                if (prev and observations >= 3 and fresh and not suspect_live
                        and drop_last >= threshold and drop_from_open >= threshold):
                    is_drop = True

                line_ops.append(UpdateOne(
                    {"_id": key},
                    {"$set": {
                        "price": curr,
                        "open_price": open_price,
                        "observations": observations,
                        "rises": rises,
                        "suspect_live": suspect_live,
                        "first_seen_at": first_seen,
                        "updated_at": now_dt.isoformat(),
                        "match_id": match_id,
                    }},
                    upsert=True,
                ))

                if is_drop:
                    drops_found += 1
                    ctx = await self._alert_market_context(match, sel, sport)
                    text = self._format_drop_alert(match, sel, prev_price, curr,
                                                   drop_last, drop_from_open, ctx)
                    tg_result = {"ok": False}
                    if not dry_run_notify:
                        try:
                            tg_result = await self.telegram.send_message(text)
                        except Exception as e:
                            tg_result = {"ok": False, "error": str(e)}
                    best_it = (ctx or {}).get("best_it")
                    betfair = (ctx or {}).get("betfair")
                    polymarket = (ctx or {}).get("polymarket")
                    kalshi = (ctx or {}).get("kalshi")
                    await self.db.alerts.insert_one({
                        "_id": str(uuid.uuid4()),
                        "type": "drop",
                        "provider": provider,
                        "sport": sport,
                        "created_at": now_dt.isoformat(),
                        "player1": match.get("player1"),
                        "player2": match.get("player2"),
                        "tournament": match.get("tournament"),
                        "start_epoch": start_epoch,
                        "market_name": sel["market_name"],
                        "label": sel["label"],
                        "prev_price": round(prev_price, 3) if prev_price else None,
                        "price": round(curr, 3),
                        "drop_last": round(drop_last, 4),
                        "drop_from_open": round(drop_from_open, 4),
                        "best_book_it": best_it.get("bookmaker") if best_it else None,
                        "best_price_it": round(best_it["price"], 3) if best_it else None,
                        "betfair_price": round(betfair, 3) if betfair else None,
                        "polymarket_price": round(polymarket, 3) if polymarket else None,
                        "kalshi_price": round(kalshi, 3) if kalshi else None,
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
                    "suspect_live": suspect_live,
                })

            if line_rows:
                meta = SPORT_META.get(sport, {})
                matches_payload.append({
                    "match_id": match_id,
                    "sport": sport,
                    "sport_label": meta.get("label", sport),
                    # Outrights carry a per-competition emoji (⚽/🎾/⛳/🏆).
                    "sport_emoji": match.get("emoji") or meta.get("emoji", ""),
                    "start_time": match.get("start_epoch"),
                    "tournament": match.get("tournament"),
                    "player1": match.get("player1"),
                    "player2": match.get("player2"),
                    "lines": line_rows,
                })

        # One round-trip for all the line-state upserts this scan produced.
        if line_ops:
            await self.db.line_state.bulk_write(line_ops, ordered=False)

        cutoff = (now_dt - timedelta(seconds=LINE_STATE_TTL_SECONDS)).isoformat()
        try:
            await self.db.line_state.delete_many({"updated_at": {"$lt": cutoff}})
        except Exception:
            logger.debug("line_state prune skipped")

        if sports is not None:
            # Partial scan (e.g. the fast F1 loop): keep the other sports'
            # matches in the shared snapshot instead of wiping them.
            existing = await self.db.snapshots.find_one({"_id": "latest"}) or {}
            kept = [m for m in existing.get("matches") or []
                    if m.get("sport") not in sports]
            matches_payload = kept + matches_payload

        await self.db.snapshots.update_one(
            {"_id": "latest"},
            {"$set": {
                "updated_at": now_dt.isoformat(),
                "provider": self.provider,
                "basketball_enabled": self.basketball_enabled,
                "football_enabled": self.football_enabled,
                "football_provider": self.football_provider,
                "f1_enabled": self.f1_enabled,
                "mlb_enabled": self.mlb_enabled,
                "hockey_enabled": self.hockey_enabled,
                "volley_enabled": self.volley_enabled,
                "outright_enabled": self.outright_enabled,
                "drop_threshold": self.drop_threshold,
                "football_drop_threshold": self.football_drop_threshold,
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

    async def _notify_ip_blocked(self, provider: str):
        if provider in self._ipblock_alerted:
            return
        self._ipblock_alerted.add(provider)
        label = PROVIDER_LABELS.get(provider, provider)
        text = (
            f"<b>🚫 {label} risponde 403 Forbidden</b>\n"
            f"Il server del monitor sembra bloccato a livello di IP (non è un "
            f"problema di key: la rotazione automatica non può risolverlo). "
            f" Gli sport su questo provider sono sospesi finché il blocco non "
            f"viene rimosso."
        )
        try:
            await self.telegram.send_message(text)
        except Exception as e:
            logger.warning("ip-block telegram alert failed: %s", e)

    async def _alert_market_context(self, match: dict, sel: dict, sport: str) -> dict | None:
        """Cross-check prices for the alerted selection, right now:
        - football: best Italy-book price + Betfair EX via The Odds API
          (one request) plus Polymarket;
        - basketball: Polymarket + Kalshi (keyless, quota-free).
        Best-effort: every failure just omits its piece from the alert."""
        start_epoch = match.get("start_epoch")
        if not start_epoch:
            return None
        home, away = match.get("player1"), match.get("player2")
        ctx: dict[str, Any] = {}
        outcome = (sel.get("outcome") or "").lower()

        async def oddsapi_lookup() -> None:
            client = self.clients.get("theoddsapi")
            if (sport != "football" or client is None or sel["market_key"] != "h2h"):
                return
            try:
                r = await client.get_alert_market_context(
                    match.get("tournament"), start_epoch, home, away,
                    sel["market_key"], outcome, sel.get("point"))
                if r:
                    ctx.update(r)
            except Exception as e:
                logger.warning("oddsapi alert context failed: %s", e)

        side = {"home": home, "away": away}.get(outcome)
        if sel["market_key"] != "h2h" or not side:
            side = None

        async def kalshi_f1_lookup() -> None:
            if sport != "f1" or sel["market_key"] != "winner":
                return
            try:
                p = await pmk.kalshi_f1_price(side or sel.get("label"), start_epoch)
                if p:
                    ctx["kalshi"] = p
            except Exception as e:
                logger.warning("kalshi f1 lookup failed: %s", e)

        async def pm_lookup() -> None:
            if not side:
                return
            try:
                p = await pmk.polymarket_price(sport, home, away, start_epoch, side)
                if p:
                    ctx["polymarket"] = p
            except Exception as e:
                logger.warning("polymarket lookup failed: %s", e)

        async def kalshi_lookup() -> None:
            if not side or sport not in pmk.KALSHI_SERIES:
                return
            try:
                p = await pmk.kalshi_price(sport, home, away, start_epoch, side)
                if p:
                    ctx["kalshi"] = p
            except Exception as e:
                logger.warning("kalshi lookup failed: %s", e)

        await asyncio.gather(oddsapi_lookup(), pm_lookup(), kalshi_lookup(),
                             kalshi_f1_lookup())
        return ctx or None

    async def _notify_quota_exhausted(self, provider: str):
        if provider in self._quota_alerted:
            return
        self._quota_alerted.add(provider)
        label = PROVIDER_LABELS.get(provider, provider)
        hint = (
            "\n💡 Genera una nuova key e impostala dalla dashboard "
            "(Settings → OddsPapi key): si applica subito, senza redeploy."
            if provider == "oddspapi" else ""
        )
        text = (
            f"<b>⚠️ Quota API esaurita — {label}</b>\n"
            f"Lo scan per gli sport che usano questo provider è sospeso finché "
            f"la quota non si resetta.{hint}"
        )
        try:
            await self.telegram.send_message(text)
        except Exception as e:
            logger.warning("quota-exhausted telegram alert failed: %s", e)

    def _format_drop_alert(self, match: dict, sel: dict, prev_price: float,
                           curr: float, drop_last: float, drop_from_open: float,
                           ctx: dict | None = None) -> str:
        start_ts = match.get("start_epoch")
        start_str = ""
        if start_ts:
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%H:%M UTC")
        meta = SPORT_META.get(match.get("sport"), {})
        sport_tag = f"{meta.get('emoji', '')} {meta.get('label', 'Tennis')}".strip()
        esc = html.escape
        ctx = ctx or {}
        best_it = ctx.get("best_it")
        betfair = ctx.get("betfair")
        polymarket = ctx.get("polymarket")
        kalshi = ctx.get("kalshi")
        extra = ""
        if best_it:
            edge = (best_it["price"] - curr) / curr * 100 if curr else 0
            extra += (f"\n🇮🇹 Miglior prezzo ITA: <b>{esc(best_it['bookmaker'])}</b> "
                      f"@ <b>{best_it['price']:.2f}</b> (Pinnacle {curr:.2f}, "
                      f"{'+' if edge >= 0 else ''}{edge:.1f}%)")
        if betfair:
            extra += f"\n📊 Betfair EX: <b>{betfair:.2f}</b>"
        pred = " · ".join(
            f"{label} <b>{p:.2f}</b>" for label, p in
            (("Polymarket", polymarket), ("Kalshi", kalshi)) if p)
        if pred:
            extra += f"\n🎯 {pred}"
        sport = match.get("sport")
        # Anything from the keyless prediction markets (F1/MLB games AND the
        # outright winner markets) is Polymarket-sourced, NOT Pinnacle - so it
        # must never be labelled "PINNACLE DROP". Key off the provider so the
        # source in the header is always right.
        is_prediction = match.get("provider") == "prediction"
        p1, p2 = match.get("player1"), match.get("player2")
        if p2 and sport == "f1" and match.get("player2") in ("Race Winner", "Podium"):
            pairing = f"{esc(str(p1))} · {esc(str(p2))}"
        elif p2:
            pairing = f"{esc(str(p1))} vs {esc(str(p2))}"
        elif p1:
            pairing = esc(str(p1))
        else:
            pairing = ""  # outrights have no two-sided pairing
        if is_prediction:
            header = "📉 STEAM Polymarket — " + esc(sport_tag)
            move_line = (f"{prev_price:.2f} → <b>{curr:.2f}</b> "
                         f"(<b>-{drop_last * 100:.1f}%</b>) · quote Polymarket")
        else:
            header = "⬇️ PINNACLE DROP — " + esc(sport_tag)
            move_line = (f"{prev_price:.2f} → <b>{curr:.2f}</b> "
                         f"(<b>-{drop_last * 100:.1f}%</b>)")
        tournament = esc(match.get("tournament") or "")
        comp_line = f"{tournament} · start {start_str}" if start_str else tournament
        lines = [f"<b>{header}</b>"]
        if pairing:
            lines.append(pairing)
        if comp_line:
            lines.append(comp_line)
        lines.append(f"{esc(sel['market_name'])} — <b>{esc(sel['label'])}</b>")
        lines.append(move_line)
        lines.append(f"da apertura: -{drop_from_open * 100:.1f}%{extra}")
        return "\n".join(lines)
